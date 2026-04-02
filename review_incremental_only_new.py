#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论增量抓取 + Supabase 入库版

逻辑：
1. 第一次运行：抓取当前全部评论，作为初始化基线，全部写入 Supabase
2. 后续运行：只识别新增评论，只把新增评论写入 Supabase
3. review_sync_state 表用于记录每个 ASIN 的同步状态
4. reviews 表用于保存评论明细
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from deep_translator import GoogleTranslator
from supabase import create_client, Client


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
BASE_URL = "https://www.woot.com/review/Reviews/"
SUPABASE_PAGE_SIZE = 1000
UPSERT_BATCH_SIZE = 200
STATE_REVIEW_KEYS_LIMIT = 200


def get_env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def create_supabase_client() -> Client:
    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_KEY")
    return create_client(url, key)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def translate_text(text: str, target_lang: str = "zh-CN") -> str:
    try:
        if not text:
            return ""
        return GoogleTranslator(source="auto", target=target_lang).translate(text)
    except Exception:
        return text


def translate_unique_texts(texts, target_lang="zh-CN", max_workers=4):
    unique_texts = []
    seen = set()

    for text in texts:
        if not text:
            continue
        if text not in seen:
            seen.add(text)
            unique_texts.append(text)

    translated_map = {}
    if not unique_texts:
        return translated_map

    worker_count = min(max_workers, max(1, len(unique_texts)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_text = {
            executor.submit(translate_text, text, target_lang): text
            for text in unique_texts
        }
        for future in as_completed(future_to_text):
            source_text = future_to_text[future]
            try:
                translated_map[source_text] = future.result()
            except Exception:
                translated_map[source_text] = source_text

    return translated_map


def fetch_reviews(asin: str, filter_val=0, sort_val=0, delay=0.2):
    url_base = BASE_URL + asin
    reviews = []
    paging_next = None

    while True:
        params = {
            "filter": str(filter_val),
            "isVerified": "false",
            "sort": str(sort_val),
        }

        if paging_next:
            params["pagingNext"] = paging_next
        else:
            params["page"] = "1"

        url = url_base + "?" + urllib.parse.urlencode(params)
        headers = {**HEADERS, "Referer": f"https://www.woot.com/review/{asin}"}
        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"[{asin}] 请求失败: {e}")
            break

        batch = data.get("Reviews", [])
        if not batch:
            break

        reviews.extend(batch)
        paging_next = data.get("PagingNext", "")
        if not paging_next:
            break

        time.sleep(delay)

    return reviews


def review_key(review: dict):
    author = str(review.get("Author", "") or "").strip()
    title = str(review.get("Title", "") or "").strip()
    text = str(review.get("Text", "") or "").strip()[:200]
    rating = str(review.get("OverallRating", "") or "").strip()
    origin = str(review.get("OriginDescription", "") or "").strip()
    return "||".join([author, title, text, rating, origin])


def scrape_basic(asin: str):
    return fetch_reviews(asin, filter_val=0, sort_val=0)


def scrape_full(asin: str):
    seen = set()
    unique = []

    for star in [5, 4, 3, 2, 1]:
        revs = fetch_reviews(asin, filter_val=star, sort_val=0)
        for review in revs:
            key = review_key(review)
            if key not in seen:
                seen.add(key)
                unique.append(review)

    return unique


def scrape_max(asin: str):
    seen = set()
    unique = []

    for star in [5, 4, 3, 2, 1]:
        for sort_val in [0, 1, 2, 3]:
            revs = fetch_reviews(asin, filter_val=star, sort_val=sort_val, delay=0.15)
            for review in revs:
                key = review_key(review)
                if key not in seen:
                    seen.add(key)
                    unique.append(review)

    return unique


def get_reviews_by_mode(asin: str, mode: str = "max"):
    mode = mode.lower()
    if mode == "basic":
        return scrape_basic(asin)
    if mode == "full":
        return scrape_full(asin)
    return scrape_max(asin)


def translate_reviews_inplace(reviews: list, translate_mode: str = "none", target_lang="zh-CN"):
    if translate_mode not in {"none", "title", "full"}:
        translate_mode = "none"

    if translate_mode in {"title", "full"}:
        titles = [str(r.get("Title", "") or "") for r in reviews]
        title_map = translate_unique_texts(titles, target_lang=target_lang)
        for r in reviews:
            src = str(r.get("Title", "") or "")
            r["Title_zh"] = title_map.get(src, src)

    if translate_mode == "full":
        texts = [str(r.get("Text", "") or "") for r in reviews]
        text_map = translate_unique_texts(texts, target_lang=target_lang)
        for r in reviews:
            src = str(r.get("Text", "") or "")
            r["Text_zh"] = text_map.get(src, src)

    return reviews


def get_existing_review_keys(supabase: Client, asin: str) -> set[str]:
    all_keys = set()
    start = 0

    while True:
        end = start + SUPABASE_PAGE_SIZE - 1
        result = (
            supabase.table("reviews")
            .select("review_key")
            .eq("asin", asin)
            .range(start, end)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        all_keys.update({row["review_key"] for row in rows if row.get("review_key")})
        if len(rows) < SUPABASE_PAGE_SIZE:
            break
        start += SUPABASE_PAGE_SIZE

    return all_keys


def to_review_rows(asin: str, reviews: list[dict], scraped_at: str) -> list[dict]:
    rows = []
    for r in reviews:
        rows.append({
            "asin": asin,
            "review_key": review_key(r),
            "author": r.get("Author", ""),
            "title": r.get("Title", ""),
            "text": r.get("Text", ""),
            "rating": r.get("OverallRating"),
            "origin_description": r.get("OriginDescription", ""),
            "title_zh": r.get("Title_zh", ""),
            "text_zh": r.get("Text_zh", ""),
            "scraped_at": scraped_at,
            "raw_payload": r,
        })
    return rows


def upsert_reviews(supabase: Client, rows: list[dict]):
    if not rows:
        return None
    total = 0
    for i in range(0, len(rows), UPSERT_BATCH_SIZE):
        chunk = rows[i:i + UPSERT_BATCH_SIZE]
        (
            supabase.table("reviews")
            .upsert(chunk, on_conflict="asin,review_key")
            .execute()
        )
        total += len(chunk)
    return {"upserted_rows": total}


def upsert_sync_state(
    supabase: Client,
    asin: str,
    current_keys: set[str],
    current_total: int,
    new_count: int,
    scraped_at: str,
):
    payload = {
        "asin": asin,
        "review_keys": sorted(list(current_keys))[:STATE_REVIEW_KEYS_LIMIT],
        "last_total_reviews": current_total,
        "last_new_count": new_count,
        "last_check_time": scraped_at,
        "last_scraped_at": scraped_at,
        "updated_at": scraped_at,
    }
    return (
        supabase.table("review_sync_state")
        .upsert(payload, on_conflict="asin")
        .execute()
    )


def incremental_update(
    supabase: Client,
    asin: str,
    current_reviews: list,
    translate_mode: str = "none",
    include_reviews: bool = False,
    sample_size: int = 5,
):
    """
    第一次运行：全部写入
    后续运行：只追加新增评论
    """
    old_keys = get_existing_review_keys(supabase, asin)

    current_key_map = {}
    for review in current_reviews:
        current_key_map[review_key(review)] = review

    current_keys = set(current_key_map.keys())

    if not old_keys:
        new_keys = current_keys
    else:
        new_keys = current_keys - old_keys

    new_reviews = [current_key_map[k] for k in new_keys if k in current_key_map]

    if translate_mode != "none" and new_reviews:
        translate_reviews_inplace(new_reviews, translate_mode=translate_mode)

    scraped_at = now_iso()
    rows = to_review_rows(asin, new_reviews, scraped_at)
    upsert_result = upsert_reviews(supabase, rows)
    upsert_sync_state(
        supabase=supabase,
        asin=asin,
        current_keys=current_keys,
        current_total=len(current_reviews),
        new_count=len(new_reviews),
        scraped_at=scraped_at,
    )

    result = {
        "asin": asin,
        "scraped_at": scraped_at,
        "current_total": len(current_reviews),
        "new_count": len(new_reviews),
        "upserted_rows": (upsert_result or {}).get("upserted_rows", 0),
        "new_reviews_sample": new_reviews[:max(0, sample_size)],
    }
    if include_reviews:
        result["new_reviews"] = new_reviews
    return result


def run_once(
    asin: str,
    mode: str = "max",
    translate_mode: str = "none",
    include_reviews: bool = False,
):
    asin = asin.strip().upper()
    supabase = create_supabase_client()

    print(f"开始抓取 ASIN: {asin}")
    reviews = get_reviews_by_mode(asin, mode=mode)
    print(f"抓取完成，当前总评论数: {len(reviews)}")

    result = incremental_update(
        supabase=supabase,
        asin=asin,
        current_reviews=reviews,
        translate_mode=translate_mode,
        include_reviews=include_reviews,
    )

    print(json.dumps({
        "asin": result["asin"],
        "current_total": result["current_total"],
        "new_count": result["new_count"],
    }, ensure_ascii=False, indent=2))

    return result


if __name__ == "__main__":
    ASIN = get_env("ASIN", required=False, default="B0G64PSMX4")
    MODE = get_env("MODE", required=False, default="max")
    TRANSLATE_MODE = get_env("TRANSLATE_MODE", required=False, default="full")

    run_once(
        asin=ASIN,
        mode=MODE,
        translate_mode=TRANSLATE_MODE,
    )
