#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi import HTTPException
from dotenv import load_dotenv
from review_incremental_supabase import run_once
from review_incremental_only_new import create_supabase_client
import traceback
import os
from datetime import datetime, timezone
from uuid import uuid4

load_dotenv()

app = FastAPI()


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


def create_task_record(task_id: str, asin: str, mode: str, translate_mode: str):
    supabase = create_supabase_client()
    payload = {
        "task_id": task_id,
        "asin": asin,
        "mode": mode,
        "translate_mode": translate_mode,
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    return supabase.table("review_sync_tasks").insert(payload).execute()


def update_task_record(task_id: str, status: str, result=None, error=None):
    supabase = create_supabase_client()
    payload = {
        "status": status,
        "updated_at": now_iso(),
        "result": result,
        "error": error,
    }
    return (
        supabase.table("review_sync_tasks")
        .update(payload)
        .eq("task_id", task_id)
        .execute()
    )


def run_sync_task(task_id: str, asin: str, mode: str, translate_mode: str):
    try:
        update_task_record(task_id=task_id, status="running")
        result = run_once(
            asin=asin,
            mode=mode,
            translate_mode=translate_mode,
            include_reviews=False,
        )
        update_task_record(task_id=task_id, status="success", result=result)
    except Exception as e:
        update_task_record(
            task_id=task_id,
            status="failed",
            error={
                "error": str(e),
                "type": e.__class__.__name__,
                "trace": traceback.format_exc(),
            },
        )


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
    include_reviews: bool = Query(False, description="Return full new_reviews list"),
):
    ensure_required_env()
    try:
        return run_once(
            asin=asin,
            mode=mode,
            translate_mode=translate_mode,
            include_reviews=include_reviews,
        )
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
    try:
        create_task_record(
            task_id=task_id,
            asin=asin,
            mode=mode,
            translate_mode=translate_mode,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "type": e.__class__.__name__,
                "hint": "Create table public.review_sync_tasks first.",
            },
        )
    background_tasks.add_task(run_sync_task, task_id, asin, mode, translate_mode)
    return {
        "task_id": task_id,
        "status": "queued",
        "check_url": f"/reviews/sync/tasks/{task_id}",
    }


@app.get("/reviews/sync/tasks/{task_id}")
def get_sync_task(task_id: str):
    ensure_required_env()
    supabase = create_supabase_client()
    result = (
        supabase.table("review_sync_tasks")
        .select("task_id,asin,mode,translate_mode,status,result,error,created_at,updated_at")
        .eq("task_id", task_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return rows[0]


@app.get("/reviews/sync/runs/new")
def get_asins_with_new_reviews(
    limit: int = Query(100, ge=1, le=500, description="Max runs to scan"),
    hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
    include_content: bool = Query(True, description="Include sample new review content"),
    sample_size: int = Query(2, ge=1, le=5, description="Sample new reviews per ASIN"),
):
    ensure_required_env()
    try:
        supabase = create_supabase_client()
        cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        result = (
            supabase.table("review_sync_runs")
            .select("asin,new_count,current_total,mode,translate_mode,created_at,status")
            .eq("status", "success")
            .gt("new_count", 0)
            .gte("created_at", cutoff_iso)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        latest_by_asin = {}
        for row in rows:
            asin = row.get("asin")
            if asin and asin not in latest_by_asin:
                latest_by_asin[asin] = row
        items = list(latest_by_asin.values())

        if include_content:
            for item in items:
                asin = item.get("asin")
                item["new_reviews_sample"] = []
                if not asin:
                    continue

                state_res = (
                    supabase.table("review_sync_state")
                    .select("last_scraped_at")
                    .eq("asin", asin)
                    .limit(1)
                    .execute()
                )
                state_rows = state_res.data or []
                if not state_rows:
                    continue

                last_scraped_at = state_rows[0].get("last_scraped_at")
                if not last_scraped_at:
                    continue

                reviews_res = (
                    supabase.table("reviews")
                    .select("title,title_zh,text,text_zh,scraped_at")
                    .eq("asin", asin)
                    .eq("scraped_at", last_scraped_at)
                    .limit(sample_size)
                    .execute()
                )
                review_rows = reviews_res.data or []
                item["new_reviews_sample"] = review_rows

        return {
            "count": len(latest_by_asin),
            "hours": hours,
            "asins_with_new_reviews": items,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "type": e.__class__.__name__,
                "trace": traceback.format_exc(),
            },
        )
