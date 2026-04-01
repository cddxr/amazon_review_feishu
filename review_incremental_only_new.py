#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评论增量抓取版
逻辑：
1. 第一次调用：抓取当前全部评论，作为初始化基线，全部写入 json
2. 后续调用：只识别新增评论，只把新增评论追加到累计 json
3. 每次返回：
   - current_total: 现在总评论数
   - new_count: 本次新增数量
   - new_reviews: 本次新增评论
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from deep_translator import GoogleTranslator


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
BASE_URL = "https://www.woot.com/review/Reviews/"


def translate_text(text: str, target_lang: str = "zh-CN") -> str:
    try:
        if not text:
            return ""
        return GoogleTranslator(source="auto", target=target_lang).translate(text)
    except Exception:
        return text


def translate_unique_texts(texts, target_lang="zh-CN", max_workers=8):
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


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def incremental_update(
    asin: str,
    current_reviews: list,
    state_file: Path,
    db_file: Path,
    translate_mode: str = "none",
):
    """
    第一次运行：全部写入
    后续运行：只追加新增评论
    """
    state = load_json(state_file, {})
    db = load_json(db_file, {"asin": asin, "all_reviews": []})

    if db.get("asin") != asin:
        db = {"asin": asin, "all_reviews": []}

    old_keys = set(state.get(asin, {}).get("review_keys", []))

    current_key_map = {}
    for review in current_reviews:
        current_key_map[review_key(review)] = review

    current_keys = set(current_key_map.keys())

    # 第一次运行：old_keys为空，则当前所有评论都算初始化数据
    if not old_keys:
        new_keys = current_keys
    else:
        new_keys = current_keys - old_keys

    new_reviews = [current_key_map[k] for k in new_keys if k in current_key_map]

    # 只对新增评论做翻译，避免重复翻译旧评论
    if translate_mode != "none" and new_reviews:
        translate_reviews_inplace(new_reviews, translate_mode=translate_mode)

    # 追加到累计库
    existing_map = {review_key(r): r for r in db.get("all_reviews", [])}
    for review in new_reviews:
        existing_map[review_key(review)] = review

    db["asin"] = asin
    db["all_reviews"] = list(existing_map.values())
    db["last_current_total"] = len(current_reviews)
    db["last_new_count"] = len(new_reviews)
    db["last_scraped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_json(db_file, db)

    state[asin] = {
        "review_keys": list(current_keys),
        "last_total_reviews": len(current_reviews),
        "last_check_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json(state_file, state)

    result = {
        "asin": asin,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "current_total": len(current_reviews),
        "new_count": len(new_reviews),
        "new_reviews": new_reviews,
    }

    return result


def run_once(
    asin: str,
    mode: str = "max",
    translate_mode: str = "none",
    output_dir: str = "output",
):
    asin = asin.strip().upper()
    output_path = Path(output_dir)

    # 🔥 防止 output 是文件导致报错
    if output_path.exists() and not output_path.is_dir():
        output_path.unlink()

    output_path.mkdir(parents=True, exist_ok=True)

    state_file = output_path / "review_count_state.json"
    db_file = output_path / f"reviews_{asin}_all.json"
    last_result_file = output_path / f"reviews_{asin}_last_result.json"

    print(f"开始抓取 ASIN: {asin}")
    reviews = get_reviews_by_mode(asin, mode=mode)
    print(f"抓取完成，当前总评论数: {len(reviews)}")

    result = incremental_update(
        asin=asin,
        current_reviews=reviews,
        state_file=state_file,
        db_file=db_file,
        translate_mode=translate_mode,
    )

    save_json(last_result_file, result)

    print(json.dumps({
        "asin": result["asin"],
        "current_total": result["current_total"],
        "new_count": result["new_count"],
        "last_result_file": str(last_result_file),
        "all_reviews_file": str(db_file),
    }, ensure_ascii=False, indent=2))

    return result


if __name__ == "__main__":
    ASIN = "B0G64PSMX4"

    run_once(
        asin=ASIN,
        mode="max",          # basic / full / max
        translate_mode="full",  # none / title / full
        output_dir="output",
    )
