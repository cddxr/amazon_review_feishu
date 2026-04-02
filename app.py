#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fastapi import FastAPI, Query
from review_incremental_supabase import run_once

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/reviews/sync")
def reviews_sync(
    asin: str = Query(..., description="Amazon ASIN"),
    mode: str = Query("max", description="basic / full / max"),
    translate_mode: str = Query("full", description="none / title / full"),
):
    return run_once(asin=asin, mode=mode, translate_mode=translate_mode)
