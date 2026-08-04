#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论增量抓取 + Supabase 入库（火山方舟版）

功能说明：
1. 第一次运行：抓取当前全部评论，作为初始化基线，并写入 Supabase
2. 后续运行：只识别新增评论，只把新增评论写入 Supabase
3. review_sync_state 表用于记录每个 ASIN 的同步状态
4. reviews 表用于保存评论明细
5. 支持新增评论的 AI 翻译 + 语义分析（summary/tags/action_suggestions）

环境变量：
- SUPABASE_URL
- SUPABASE_KEY
- ARK_API_KEY（TRANSLATE_MODE != none 时必需）
- ARK_MODEL（可选，默认 doubao-1-5-lite-32k-250115）
- ARK_BASE_URL（可选，默认 https://ark.cn-beijing.volces.com/api/v3）
- ASIN（可选）
- MODE（可选，默认 max）
- TRANSLATE_MODE（可选，默认 full，none/title/full）
"""

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import requests
from supabase import Client, create_client


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

ARK_BASE_URL_DEFAULT = "https://ark.cn-beijing.volces.com/api/v3"
ARK_MODEL_DEFAULT = "doubao-seed-2-0-lite-260428"
ARK_RETRIES = 3
ARK_REQUEST_TIMEOUT = 30
ARK_REQUEST_INTERVAL = 0.12
MAX_SUGGESTIONS = 3
ALLOWED_TAGS = [
    "气味",
    "味道",
    "价格",
    "功效",
    "性价比",
    "包装",
    "物流",
    "服务",
    "质量",
    "安全性",
]


class ArkReviewProcessor:
    def __init__(self):
        self.api_key = get_env("ARK_API_KEY")
        self.model = get_env("ARK_MODEL", required=False, default=ARK_MODEL_DEFAULT)
        self.base_url = get_env("ARK_BASE_URL", required=False, default=ARK_BASE_URL_DEFAULT)

    def _chat_url(self) -> str:
        base = (self.base_url or ARK_BASE_URL_DEFAULT).rstrip("/")
        if base.endswith("/responses"):
            return base
        return f"{base}/responses"

    @staticmethod
    def _extract_json(content: str) -> dict:
        content = (content or "").strip()
        if not content:
            return {}

        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise RuntimeError(f"model response is not valid json object: {content[:300]}")

        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"model response is not json object: {content[:300]}")
        return parsed

    def _chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Ark responses API style:
        # https://ark.cn-beijing.volces.com/api/v3/responses
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"{system_prompt}\n\n{user_prompt}",
                        }
                    ],
                }
            ],
        }

        last_error: Optional[Exception] = None
        for attempt in range(ARK_RETRIES):
            try:
                response = requests.post(
                    self._chat_url(),
                    headers=headers,
                    json=payload,
                    timeout=ARK_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()
                content = data.get("output_text", "")
                if not content:
                    # Fallback for non-flattened response payloads.
                    outputs = data.get("output", []) or []
                    chunks = []
                    for item in outputs:
                        for c in item.get("content", []) or []:
                            text = c.get("text")
                            if text:
                                chunks.append(text)
                    content = "\n".join(chunks).strip()
                if not content:
                    raise RuntimeError(f"Ark empty response: {data}")
                return self._extract_json(content)
            except Exception as e:
                last_error = e
                if attempt < ARK_RETRIES - 1:
                    time.sleep(1.2 * (attempt + 1))

        raise RuntimeError(f"Ark request failed after {ARK_RETRIES} retries: {last_error}")

    def process_review(
        self,
        title: str,
        text: str,
        translate_mode: str = "full",
        target_lang: str = "ZH",
    ) -> dict:
        title = normalize_text(title)
        text = normalize_text(text)

        system_prompt = (
            "你是电商评论分析助手。只返回 JSON，不要返回解释。"
            "请基于评论原文识别用户提到的主题标签，并给出可执行建议。"
        )

        if translate_mode == "none":
            translation_rule = "translated_title_zh 和 translated_text_zh 必须返回空字符串。"
        elif translate_mode == "title":
            translation_rule = "translated_title_zh 返回中文标题翻译；translated_text_zh 返回空字符串。"
        else:
            translation_rule = "translated_title_zh 和 translated_text_zh 都返回中文翻译。"

        user_prompt = f"""
请分析以下评论并返回 JSON。

标签候选（仅可从以下标签中选择）：{json.dumps(ALLOWED_TAGS, ensure_ascii=False)}
判定规则：只输出评论文本中明确提及或可直接推断的标签，不要硬凑。

返回字段要求：
- translated_title_zh: string
- translated_text_zh: string
- summary_zh: string（不超过60字）
- tags: string[]
- action_suggestions: string[]（1-3条，中文，针对性建议）

额外要求：
- {translation_rule}
- tags 去重，保持简洁。
- action_suggestions 需要与 tags 对应。

target_lang={target_lang}
title={json.dumps(title, ensure_ascii=False)}
text={json.dumps(text, ensure_ascii=False)}
""".strip()

        parsed = self._chat_json(system_prompt=system_prompt, user_prompt=user_prompt)

        raw_tags = parsed.get("tags")
        if not isinstance(raw_tags, list):
            raw_tags = []

        allowed_set = set(ALLOWED_TAGS)
        tags = []
        seen_tags = set()
        for tag in raw_tags:
            value = normalize_text(tag)
            if not value or value not in allowed_set or value in seen_tags:
                continue
            seen_tags.add(value)
            tags.append(value)

        raw_suggestions = parsed.get("action_suggestions")
        if not isinstance(raw_suggestions, list):
            raw_suggestions = []

        action_suggestions = []
        for item in raw_suggestions:
            value = normalize_text(item)
            if value:
                action_suggestions.append(value)
            if len(action_suggestions) >= MAX_SUGGESTIONS:
                break

        return {
            "Title_zh": normalize_text(parsed.get("translated_title_zh", "")),
            "Text_zh": normalize_text(parsed.get("translated_text_zh", "")),
            "summary_zh": normalize_text(parsed.get("summary_zh", "")),
            "tags": tags,
            "action_suggestions": action_suggestions,
        }


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


def review_date_sort_key(review: dict) -> float:
    origin = normalize_text(review.get("OriginDescription", ""))
    match = re.search(r"\bon\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*$", origin)
    if match:
        try:
            return datetime.strptime(match.group(1), "%B %d, %Y").timestamp()
        except ValueError:
            pass

    raw_date = normalize_text(review.get("SubmissionDateStr", ""))
    for date_format in ("%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw_date, date_format)
            if parsed.year > 1970:
                return parsed.timestamp()
        except ValueError:
            continue
    return 0.0


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


def enrich_reviews_inplace(
    reviews: list,
    processor: ArkReviewProcessor,
    translate_mode: str = "none",
):
    if translate_mode not in {"none", "title", "full"}:
        translate_mode = "none"

    for idx, review in enumerate(reviews, start=1):
        title = normalize_text(review.get("Title", ""))
        text = normalize_text(review.get("Text", ""))

        if not title and not text:
            review["Title_zh"] = ""
            review["Text_zh"] = ""
            review["summary_zh"] = ""
            review["tags"] = []
            review["action_suggestions"] = []
            continue

        try:
            enriched = processor.process_review(
                title=title,
                text=text,
                translate_mode=translate_mode,
                target_lang="ZH",
            )
            review.update(enriched)
        except Exception as e:
            print(f"enrich review failed idx={idx}, fallback to empty ai fields, error={e}")
            # Never copy the source text into translation fields. The Feishu
            # renderer displays both fields, so copying creates duplicates.
            review["Title_zh"] = ""
            review["Text_zh"] = ""
            review["summary_zh"] = ""
            review["tags"] = []
            review["action_suggestions"] = []

        time.sleep(ARK_REQUEST_INTERVAL)

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
            "summary_zh": r.get("summary_zh") or "",
            "tags": r.get("tags") or [],
            "action_suggestions": r.get("action_suggestions") or [],
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
    processor: Optional[ArkReviewProcessor],
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

    is_initial_sync = not old_keys
    new_keys = set() if is_initial_sync else current_keys - old_keys

    new_reviews = []
    seen_new_keys = set()
    for review in current_reviews:
        key = review_key(review)
        if key in new_keys and key not in seen_new_keys:
            seen_new_keys.add(key)
            new_reviews.append(review)
    new_reviews.sort(key=review_date_sort_key, reverse=True)
    reviews_to_store = list(current_key_map.values()) if is_initial_sync else new_reviews

    if translate_mode != "none" and new_reviews:
        if processor is None:
            raise RuntimeError("TRANSLATE_MODE 不是 none，但未初始化 ArkReviewProcessor")
        enrich_reviews_inplace(
            new_reviews,
            processor=processor,
            translate_mode=translate_mode,
        )

    scraped_at = now_iso()
    rows = to_review_rows(asin, reviews_to_store, scraped_at)
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
        "initial_sync": is_initial_sync,
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
        processor = ArkReviewProcessor()
    else:
        processor = None

    print(f"开始抓取 ASIN: {asin}")
    reviews = get_reviews_by_mode(asin, mode=mode)
    print(f"抓取完成，当前总评论数: {len(reviews)}")

    try:
        result = incremental_update(
            supabase=supabase,
            asin=asin,
            current_reviews=reviews,
            processor=processor,
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
    ASIN = get_env("ASIN", required=False, default="B0FRSHR4CP")
    MODE = get_env("MODE", required=False, default="max")
    TRANSLATE_MODE = get_env("TRANSLATE_MODE", required=False, default="full")

    run_once(
        asin=ASIN,
        mode=MODE,
        translate_mode=TRANSLATE_MODE,
    )
