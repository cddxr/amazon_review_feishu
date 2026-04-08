#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论增量抓取 + Supabase 入库（DeepL 稳定版）

功能说明：
1. 第一次运行：抓取当前全部评论，作为初始化基线，并写入 Supabase
2. 后续运行：只识别新增评论，只把新增评论写入 Supabase
3. review_sync_state 表用于记录每个 ASIN 的同步状态
4. reviews 表用于保存评论明细
5. 支持标题 + 正文完整翻译（TRANSLATE_MODE=full）
6. 使用 DeepL 官方接口

环境变量：
- SUPABASE_URL
- SUPABASE_KEY
- DEEPL_API_KEY
- ASIN（可选）
- MODE（可选，默认 max）
- TRANSLATE_MODE（可选，默认 full）
"""

import json
import os
import time
import urllib.parse
import urllib.request
import html
from datetime import datetime, timezone
from typing import Optional

import requests
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
HTTP_RETRIES = 4

# ===== DeepL 翻译配置 =====
TRANSLATION_BATCH_SIZE = 20
TRANSLATION_RETRIES = 5
TRANSLATION_RETRY_DELAY = 1.5
TRANSLATION_REQUEST_TIMEOUT = 30
TRANSLATION_REQUEST_INTERVAL = 0.3
DEFAULT_TARGET_LANG = "ZH"


class DeepLTranslator:
    def __init__(self):
        self.key = get_env("DEEPL_API_KEY")
        self.url = "https://api-free.deepl.com/v2/translate"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str = DEFAULT_TARGET_LANG,
        source_lang: Optional[str] = None,
    ) -> list[str]:
        if not texts:
            return []

        last_error = None

        for attempt in range(TRANSLATION_RETRIES):
            try:
                data = [
                    ("auth_key", self.key),
                    ("target_lang", target_lang),
                ]

                if source_lang:
                    data.append(("source_lang", source_lang))

                for text in texts:
                    data.append(("text", text))

                response = requests.post(
                    self.url,
                    data=data,
                    timeout=TRANSLATION_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                translations = result.get("translations", [])
                if not isinstance(translations, list):
                    raise RuntimeError(f"DeepL 返回格式异常: {result}")

                return [str(item.get("text", "")).strip() for item in translations]

            except Exception as e:
                last_error = e
                if attempt < TRANSLATION_RETRIES - 1:
                    time.sleep(TRANSLATION_RETRY_DELAY * (attempt + 1))

        raise RuntimeError(f"DeepL translation failed after {TRANSLATION_RETRIES} retries: {last_error}")


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


def fetch_reviews(asin: str, filter_val=0, sort_val=0, is_verified=False, delay=0.2):
    url_base = BASE_URL + asin
    reviews = []
    paging_next = None

    while True:
        params = {
            "filter": str(filter_val),
            "isVerified": "true" if is_verified else "false",
            "sort": str(sort_val),
        }

        if paging_next:
            params["pagingNext"] = paging_next
        else:
            params["page"] = "1"

        url = url_base + "?" + urllib.parse.urlencode(params)
        headers = {**HEADERS, "Referer": f"https://www.woot.com/review/{asin}"}
        req = urllib.request.Request(url, headers=headers)

        data = None
        for attempt in range(HTTP_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                break
            except Exception as e:
                if attempt == HTTP_RETRIES - 1:
                    print(
                        f"[{asin}] request failed filter={filter_val} sort={sort_val} "
                        f"isVerified={is_verified} error={e}"
                    )
                    return reviews
                time.sleep(0.8 * (attempt + 1))

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

    for is_verified in [False, True]:
        for star in [0, 5, 4, 3, 2, 1]:
            revs = fetch_reviews(
                asin,
                filter_val=star,
                sort_val=0,
                is_verified=is_verified,
            )
            for review in revs:
                key = review_key(review)
                if key not in seen:
                    seen.add(key)
                    unique.append(review)

    return unique


def scrape_max(asin: str):
    seen = set()
    unique = []

    for is_verified in [False, True]:
        for star in [0, 5, 4, 3, 2, 1]:
            for sort_val in [0, 1, 2, 3]:
                revs = fetch_reviews(
                    asin,
                    filter_val=star,
                    sort_val=sort_val,
                    is_verified=is_verified,
                    delay=0.12,
                )
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


def normalize_text(value) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).strip()


def translate_unique_texts(
    texts: list[str],
    translator: DeepLTranslator,
    target_lang: str = DEFAULT_TARGET_LANG,
    batch_size: int = TRANSLATION_BATCH_SIZE,
) -> dict[str, str]:
    unique_texts = []
    seen = set()

    for text in texts:
        text = normalize_text(text)
        if not text:
            continue
        if not any(ch.isalpha() for ch in text):
            continue
        if text not in seen:
            seen.add(text)
            unique_texts.append(text)

    translated_map = {}
    if not unique_texts:
        return translated_map

    for i in range(0, len(unique_texts), batch_size):
        chunk = unique_texts[i:i + batch_size]
        try:
            translated_chunk = translator.translate_batch(chunk, target_lang=target_lang)
            for src, dst in zip(chunk, translated_chunk):
                translated_map[src] = dst or src
        except Exception as e:
            print(f"translation batch failed, fallback to original text, error={e}")
            for src in chunk:
                translated_map[src] = src

        time.sleep(TRANSLATION_REQUEST_INTERVAL)

    return translated_map


def translate_reviews_inplace(
    reviews: list,
    translator: DeepLTranslator,
    translate_mode: str = "none",
    target_lang: str = DEFAULT_TARGET_LANG,
):
    if translate_mode not in {"none", "title", "full"}:
        translate_mode = "none"

    if translate_mode in {"title", "full"}:
        titles = [normalize_text(r.get("Title", "")) for r in reviews]
        title_map = translate_unique_texts(
            titles,
            translator=translator,
            target_lang=target_lang,
        )
        for r in reviews:
            src = normalize_text(r.get("Title", ""))
            r["Title_zh"] = title_map.get(src, src) if src else ""

    if translate_mode == "full":
        texts = [normalize_text(r.get("Text", "")) for r in reviews]
        text_map = translate_unique_texts(
            texts,
            translator=translator,
            target_lang=target_lang,
        )
        for r in reviews:
            src = normalize_text(r.get("Text", ""))
            r["Text_zh"] = text_map.get(src, src) if src else ""

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
            "title_zh": r.get("Title_zh") or "",
            "text_zh": r.get("Text_zh") or "",
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
            .upsert(
                chunk,
                on_conflict="asin,review_key",
                ignore_duplicates=True,
            )
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


def insert_sync_run(
    supabase: Client,
    asin: str,
    mode: str,
    translate_mode: str,
    current_total: int,
    new_count: int,
    upserted_rows: int,
    status: str = "success",
    error: str | None = None,
):
    payload = {
        "asin": asin,
        "mode": mode,
        "translate_mode": translate_mode,
        "current_total": current_total,
        "new_count": new_count,
        "upserted_rows": upserted_rows,
        "status": status,
        "error": error or "",
    }
    return supabase.table("review_sync_runs").insert(payload).execute()


def incremental_update(
    supabase: Client,
    asin: str,
    current_reviews: list,
    translator: Optional[DeepLTranslator],
    mode: str = "max",
    translate_mode: str = "none",
    include_reviews: bool = False,
    sample_size: int = 5,
):
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
        if translator is None:
            raise RuntimeError("TRANSLATE_MODE 不是 none，但未初始化 DeepL Translator")
        translate_reviews_inplace(
            new_reviews,
            translator=translator,
            translate_mode=translate_mode,
        )

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

    try:
        insert_sync_run(
            supabase=supabase,
            asin=asin,
            mode=mode,
            translate_mode=translate_mode,
            current_total=len(current_reviews),
            new_count=len(new_reviews),
            upserted_rows=(upsert_result or {}).get("upserted_rows", 0),
            status="success",
        )
    except Exception as log_err:
        print(f"[{asin}] failed to write review_sync_runs log: {log_err}")

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
    translate_mode: str = "full",
    include_reviews: bool = False,
):
    asin = asin.strip().upper()
    supabase = create_supabase_client()

    if translate_mode != "none":
        try:
            translator = DeepLTranslator()
        except Exception as e:
            print(f"⚠️ DeepL 未配置，自动关闭翻译，error={e}")
            translator = None
            translate_mode = "none"
    else:
        translator = None

    print(f"开始抓取 ASIN: {asin}")
    reviews = get_reviews_by_mode(asin, mode=mode)
    print(f"抓取完成，当前总评论数: {len(reviews)}")

    try:
        result = incremental_update(
            supabase=supabase,
            asin=asin,
            current_reviews=reviews,
            translator=translator,
            mode=mode,
            translate_mode=translate_mode,
            include_reviews=include_reviews,
        )
    except Exception as e:
        try:
            insert_sync_run(
                supabase=supabase,
                asin=asin,
                mode=mode,
                translate_mode=translate_mode,
                current_total=len(reviews),
                new_count=0,
                upserted_rows=0,
                status="failed",
                error=str(e),
            )
        except Exception as log_err:
            print(f"[{asin}] failed to write review_sync_runs log: {log_err}")
        raise

    print(json.dumps({
        "asin": result["asin"],
        "current_total": result["current_total"],
        "new_count": result["new_count"],
        "upserted_rows": result["upserted_rows"],
        "translate_mode": translate_mode,
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
