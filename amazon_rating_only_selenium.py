#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import html
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


PRODUCTS = {
    "XG002": "B0FRSHR4CP",
}

WAIT_LONG = 25
WAIT_SHORT = 8
RETRIES_PER_ASIN = 3
NOT_FOUND = "未找到"
WOOT_REVIEWS_URL = "https://www.woot.com/review/Reviews/"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-agent={HTTP_HEADERS['User-Agent']}")
    options.add_argument("--lang=en-US")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary

    chrome_driver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if chrome_driver_path:
        return webdriver.Chrome(service=Service(chrome_driver_path), options=options)

    # Fallback: use Selenium Manager (works in many CI environments).
    return webdriver.Chrome(options=options)


def set_amazon_zipcode(driver: webdriver.Chrome, zipcode: str = "10001") -> None:
    driver.get("https://www.amazon.com/")
    wait = WebDriverWait(driver, WAIT_LONG)

    try:
        loc_btn = wait.until(
            EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
        )
        loc_btn.click()
    except Exception:
        return

    zip_input = wait.until(
        EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput"))
    )
    zip_input.clear()
    zip_input.send_keys(zipcode)
    driver.find_element(By.ID, "GLUXZipUpdate").click()

    try:
        time.sleep(1)
        driver.find_element(By.NAME, "glowDoneButton").click()
    except Exception:
        pass
    time.sleep(2)


def handle_continue_pages(driver: webdriver.Chrome) -> None:
    # Handle common interstitial pages without failing the run.
    for _ in range(2):
        try:
            buttons = driver.find_elements(
                By.XPATH,
                "//button[contains(., 'Continue')] | //a[contains(., 'Continue')]",
            )
            clicked = False
            for btn in buttons:
                if btn.is_displayed():
                    btn.click()
                    clicked = True
                    time.sleep(1)
                    break
            if not clicked:
                break
        except Exception:
            break


def find_text(driver: webdriver.Chrome, selectors: list[tuple[str, str]]) -> str:
    for by, selector in selectors:
        try:
            el = WebDriverWait(driver, WAIT_SHORT).until(
                EC.presence_of_element_located((by, selector))
            )
            text = (el.text or "").strip()
            if not text:
                for attr in ("aria-label", "title", "data-tooltip", "textContent"):
                    try:
                        text = (el.get_attribute(attr) or "").strip()
                    except Exception:
                        continue
                    if text:
                        break
            if text:
                return text
        except Exception:
            continue
    return NOT_FOUND


def parse_first_number(text: str) -> str:
    m = re.search(r"(\d+([.,]\d+)?)", text)
    return m.group(1) if m else text


def extract_rating_from_html(page_source: str) -> str:
    """Extract the product rating from Amazon HTML/embedded JSON."""
    source = html.unescape(page_source or "")
    patterns = [
        r'"ratingValue"\s*:\s*"?([0-5](?:\.\d+)?)',
        r'"averageStarRating"\s*:\s*"?([0-5](?:\.\d+)?)',
        r'([0-5](?:\.\d+)?)\s+out of 5 stars',
        r'([0-5](?:\.\d+)?)\s+von 5 Sternen',
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            return match.group(1)
    return NOT_FOUND


def extract_review_count_from_html(page_source: str) -> str:
    """Extract rating/review count from Amazon HTML/embedded JSON."""
    source = html.unescape(page_source or "")
    patterns = [
        r'"reviewCount"\s*:\s*"?([\d,]+)',
        r'"ratingCount"\s*:\s*"?([\d,]+)',
        r'"totalReviewCount"\s*:\s*"?([\d,]+)',
        r'id=["\']acrCustomerReviewText["\'][^>]*>\s*([\d,]+)',
        r'([\d,]+)\s+(?:global\s+)?ratings?',
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            return match.group(1).replace(",", "")
    return NOT_FOUND


def fetch_review_rating_fallback(asin: str) -> tuple[str, str]:
    """Calculate a rating/count fallback from the review feed used by sync."""
    ratings: list[float] = []
    seen: set[str] = set()
    paging_next = ""

    for _ in range(100):
        params = {"filter": "0", "isVerified": "false", "sort": "0"}
        if paging_next:
            params["pagingNext"] = paging_next
        else:
            params["page"] = "1"
        url = f"{WOOT_REVIEWS_URL}{asin}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={**HTTP_HEADERS, "Accept": "application/json", "Referer": f"https://www.woot.com/review/{asin}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        batch = payload.get("Reviews", []) or []
        if not batch:
            break
        for review in batch:
            key = str(
                review.get("ReviewId")
                or review.get("Id")
                or "||".join(
                    str(review.get(field, "") or "")
                    for field in ("Author", "Title", "Text", "OverallRating")
                )
            )
            if key in seen:
                continue
            seen.add(key)
            try:
                ratings.append(float(review.get("OverallRating")))
            except (TypeError, ValueError):
                pass

        paging_next = str(payload.get("PagingNext", "") or "")
        if not paging_next:
            break

    if not ratings:
        return NOT_FOUND, str(len(seen)) if seen else NOT_FOUND
    average = sum(ratings) / len(ratings)
    return f"{average:.1f}", str(len(seen))


def scrape_rating_and_reviews(driver: webdriver.Chrome, asin: str) -> dict:
    url = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
    driver.get(url)
    time.sleep(1)
    handle_continue_pages(driver)

    page_src = (driver.page_source or "").lower()
    bot_challenge = "enter the characters you see below" in page_src

    rating_text = find_text(
        driver,
        [
            (By.CSS_SELECTOR, "span[data-hook='rating-out-of-text']"),
            (By.CSS_SELECTOR, "i[data-hook='average-star-rating'] .a-icon-alt"),
            (By.CSS_SELECTOR, "#averageCustomerReviews .a-icon-alt"),
            (By.CSS_SELECTOR, "#acrPopover .a-size-base.a-color-base"),
            (By.CSS_SELECTOR, "#acrPopover .a-icon-alt"),
            (By.CSS_SELECTOR, "span.a-icon-alt"),
        ],
    )
    page_src = driver.page_source or ""
    if rating_text == NOT_FOUND:
        rating_text = extract_rating_from_html(page_src)

    review_text = find_text(
        driver,
        [
            (By.ID, "acrCustomerReviewText"),
            (By.CSS_SELECTOR, "a[data-hook='see-all-reviews-link-foot']"),
        ],
    )
    if review_text == NOT_FOUND:
        review_text = extract_review_count_from_html(page_src)

    result = {
        "asin": asin,
        "rating_raw": rating_text,
        "rating_value": parse_first_number(rating_text),
        "reviews_raw": review_text,
    }
    if bot_challenge:
        result["warning"] = "Amazon anti-bot challenge detected; fallback may be used"
    return result


def scrape_with_retry(driver: webdriver.Chrome, asin: str) -> dict:
    last_error = None
    last_result = None
    for attempt in range(RETRIES_PER_ASIN):
        try:
            last_result = scrape_rating_and_reviews(driver, asin)
            if last_result.get("rating_value") != NOT_FOUND:
                return last_result
            last_error = last_result.get("warning") or "Amazon rating was not found"
        except Exception as e:
            last_error = str(e)
        time.sleep(1.5 * (attempt + 1))

    try:
        fallback_rating, fallback_count = fetch_review_rating_fallback(asin)
    except Exception as exc:
        fallback_rating, fallback_count = NOT_FOUND, NOT_FOUND
        last_error = f"{last_error or 'Amazon lookup failed'}; review fallback failed: {exc}"

    result = last_result or {
        "asin": asin,
        "rating_raw": NOT_FOUND,
        "rating_value": NOT_FOUND,
        "reviews_raw": NOT_FOUND,
    }
    if result.get("rating_value") == NOT_FOUND and fallback_rating != NOT_FOUND:
        result["rating_raw"] = fallback_rating
        result["rating_value"] = fallback_rating
        result["rating_source"] = "review_average_fallback"
    if result.get("reviews_raw") == NOT_FOUND and fallback_count != NOT_FOUND:
        result["reviews_raw"] = fallback_count
    if result.get("rating_value") == NOT_FOUND:
        result["error"] = last_error or "unknown error"
    return result


def main():
    driver = build_driver()
    try:
        zipcode = os.getenv("AMAZON_ZIPCODE", "").strip()
        if zipcode:
            # Optional only. Default path skips zipcode to reduce flakiness in CI.
            set_amazon_zipcode(driver, zipcode=zipcode)

        items = []
        for name, asin in PRODUCTS.items():
            result = scrape_with_retry(driver, asin)
            items.append({"name": name, **result})

        output = {
            "timestamp": datetime.now().isoformat(),
            "zipcode": zipcode or None,
            "items": items,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
