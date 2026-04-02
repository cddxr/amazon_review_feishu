#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fastapi import FastAPI, Query
from fastapi import HTTPException
from dotenv import load_dotenv
from review_incremental_supabase import run_once
import traceback
import os

load_dotenv()

app = FastAPI()


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
