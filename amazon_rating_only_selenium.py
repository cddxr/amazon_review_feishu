#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import html
import os
import re
import time
import urllib.request
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
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
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    if os.getenv("HEADLESS", "1").strip() != "0":
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-agent={HTTP_HEADERS['User-Agent']}")
    options.add_argument("--lang=en-US")

    profile_dir = os.getenv("AMAZON_PROFILE_DIR", "").strip()
    if profile_dir:
        options.add_argument(f"--user-data-dir={os.path.abspath(profile_dir)}")

    proxy_server = os.getenv("AMAZON_PROXY_SERVER", "").strip()
    if proxy_server:
        options.add_argument(f"--proxy-server={proxy_server}")

    chrome_binary = (
        os.getenv("CHROME_BINARY_PATH", "").strip()
        or os.getenv("CHROME_BINARY", "").strip()
    )
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
        r'id=["\']acrCustomerReviewText["\'][^>]*aria-label=["\']([\d,]+)\s+Reviews?',
        r'id=["\']acrCustomerReviewText["\'][^>]*>\s*\(?([\d,]+)\)?',
        r'([\d,]+)\s+(?:global\s+)?ratings?',
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            return match.group(1).replace(",", "")
    return NOT_FOUND


def extract_customer_review_count(*texts: str) -> str:
    """Extract the written-review count from the logged-in Portal page."""
    patterns = [
        r"(?<![\d,])(\d[\d,]*)\s+customer reviews?",
        r"(?<![\d,])(\d[\d,]*)\s+with reviews?",
    ]
    for text in texts:
        source = html.unescape(text or "")
        for pattern in patterns:
            match = re.search(pattern, source, re.I)
            if match:
                return match.group(1).replace(",", "")
    return NOT_FOUND


def scrape_customer_review_count(driver: webdriver.Chrome, asin: str) -> str:
    url = (
        f"https://www.amazon.com/portal/customer-reviews/{asin}/"
        "ref=cm_cr_arp_d_viewopt_sr?ie=UTF8&filterByStar=all_stars"
        "&reviewerType=all_reviews&sortBy=recent#reviews-filter-bar"
    )
    driver.set_page_load_timeout(35)
    try:
        driver.get(url)
    except TimeoutException:
        # Amazon may leave background resources loading after the useful DOM exists.
        pass
    handle_continue_pages(driver)

    for _ in range(4):
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""
        count = extract_customer_review_count(body_text, driver.page_source or "")
        if count != NOT_FOUND:
            return count
    return NOT_FOUND


def fetch_amazon_frontend_snapshot(asin: str) -> dict:
    """Read rating and review count only from public Amazon frontend HTML."""
    urls = [
        f"https://www.amazon.com/dp/{asin}?th=1&psc=1",
        f"https://www.amazon.com/gp/product/{asin}?th=1&psc=1",
    ]
    best = {
        "asin": asin,
        "rating_raw": NOT_FOUND,
        "rating_value": NOT_FOUND,
        "reviews_raw": NOT_FOUND,
        "customer_reviews_raw": NOT_FOUND,
        "rating_source": "amazon_frontend",
    }
    errors = []

    for url in urls:
        try:
            request = urllib.request.Request(
                url,
                headers={**HTTP_HEADERS, "Referer": "https://www.amazon.com/"},
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                page_source = response.read().decode("utf-8", errors="replace")

            if "enter the characters you see below" in page_source.lower():
                errors.append(f"{url}: Amazon anti-bot challenge")
                continue

            rating = extract_rating_from_html(page_source)
            review_count = extract_review_count_from_html(page_source)
            if rating != NOT_FOUND:
                best["rating_raw"] = rating
                best["rating_value"] = rating
            if review_count != NOT_FOUND:
                best["reviews_raw"] = review_count
            if rating != NOT_FOUND and review_count != NOT_FOUND:
                best["frontend_url"] = url
                return best
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    if errors:
        best["error"] = "; ".join(errors)
    return best


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
    frontend_result = fetch_amazon_frontend_snapshot(asin)
    last_result = frontend_result
    for attempt in range(RETRIES_PER_ASIN):
        try:
            if (
                frontend_result.get("rating_value") == NOT_FOUND
                or frontend_result.get("reviews_raw") == NOT_FOUND
            ):
                browser_result = scrape_rating_and_reviews(driver, asin)
                if frontend_result.get("rating_value") == NOT_FOUND:
                    frontend_result["rating_raw"] = browser_result.get("rating_raw", NOT_FOUND)
                    frontend_result["rating_value"] = browser_result.get("rating_value", NOT_FOUND)
                if frontend_result.get("reviews_raw") == NOT_FOUND:
                    frontend_result["reviews_raw"] = browser_result.get("reviews_raw", NOT_FOUND)

            frontend_result["customer_reviews_raw"] = scrape_customer_review_count(
                driver, asin
            )
            last_result = frontend_result
            if (
                last_result.get("rating_value") != NOT_FOUND
                and last_result.get("reviews_raw") != NOT_FOUND
                and last_result.get("customer_reviews_raw") != NOT_FOUND
            ):
                return last_result
            last_error = "Amazon frontend rating or customer-review count was not found"
        except Exception as e:
            last_error = str(e)
        time.sleep(1.5 * (attempt + 1))

    result = last_result or {
        "asin": asin,
        "rating_raw": NOT_FOUND,
        "rating_value": NOT_FOUND,
        "reviews_raw": NOT_FOUND,
        "customer_reviews_raw": NOT_FOUND,
        "rating_source": "amazon_frontend",
    }
    result["error"] = last_error or result.get("error") or "Amazon frontend data not found"
    return result


def main():
    driver = build_driver()
    try:
        if os.getenv("AMAZON_LOGIN_BEFORE_SCRAPING", "0").strip() == "1":
            driver.get("https://www.amazon.com/")
            input("请在打开的 Chrome 中完成 Amazon 登录，然后回到终端按 Enter：")

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
