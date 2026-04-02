#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi import HTTPException
from dotenv import load_dotenv
from review_incremental_supabase import run_once
import traceback
import os
from datetime import datetime, timezone
from uuid import uuid4

load_dotenv()

app = FastAPI()
TASKS = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_required_env():
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_KEY") if not os.getenv(k)]
    if missing:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Missing required environment variables",
                "missing": missing,
            },
        )


def run_sync_task(task_id: str, asin: str, mode: str, translate_mode: str):
    try:
        TASKS[task_id]["status"] = "running"
        TASKS[task_id]["updated_at"] = now_iso()
        result = run_once(asin=asin, mode=mode, translate_mode=translate_mode)
        TASKS[task_id]["status"] = "success"
        TASKS[task_id]["result"] = result
        TASKS[task_id]["updated_at"] = now_iso()
    except Exception as e:
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = {
            "error": str(e),
            "type": e.__class__.__name__,
            "trace": traceback.format_exc(),
        }
        TASKS[task_id]["updated_at"] = now_iso()


@app.get("/health")
def health():
    return {"ok": True}

@app.get("/config/check")
def config_check():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    return {
        "SUPABASE_URL_set": bool(url),
        "SUPABASE_KEY_set": bool(key),
        "SUPABASE_URL_preview": (url[:40] + "...") if url else None,
        "SUPABASE_KEY_prefix": key[:12] if key else None,
    }


@app.get("/reviews/sync")
def reviews_sync(
    asin: str = Query(..., description="Amazon ASIN"),
    mode: str = Query("max", description="basic / full / max"),
    translate_mode: str = Query("full", description="none / title / full"),
):
    ensure_required_env()
    try:
        return run_once(asin=asin, mode=mode, translate_mode=translate_mode)
    except Exception as e:
        # Return readable error details for quick deployment debugging.
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "type": e.__class__.__name__,
                "trace": traceback.format_exc(),
            },
        )


@app.post("/reviews/sync/async")
def reviews_sync_async(
    background_tasks: BackgroundTasks,
    asin: str = Query(..., description="Amazon ASIN"),
    mode: str = Query("max", description="basic / full / max"),
    translate_mode: str = Query("full", description="none / title / full"),
):
    ensure_required_env()
    task_id = str(uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "params": {
            "asin": asin,
            "mode": mode,
            "translate_mode": translate_mode,
        },
        "result": None,
        "error": None,
    }
    background_tasks.add_task(run_sync_task, task_id, asin, mode, translate_mode)
    return {
        "task_id": task_id,
        "status": "queued",
        "check_url": f"/reviews/sync/tasks/{task_id}",
    }


@app.get("/reviews/sync/tasks/{task_id}")
def get_sync_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return task
