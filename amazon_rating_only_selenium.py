#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


PRODUCTS = {
    "VP203": "B0DDCJFFBM",
    "VP204": "B0DFBMVX7T",
    "VP218": "B0F8HXNY5N",
    "XG001": "B0G64PSMX4",
}

WAIT_LONG = 25
WAIT_SHORT = 8
RETRIES_PER_ASIN = 3


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

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

    loc_btn = wait.until(
        EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
    )
    loc_btn.click()

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
            if text:
                return text
        except Exception:
            continue
    return "未找到"


def parse_first_number(text: str) -> str:
    m = re.search(r"(\d+([.,]\d+)?)", text)
    return m.group(1) if m else text


def scrape_rating_and_reviews(driver: webdriver.Chrome, asin: str) -> dict:
    url = f"https://www.amazon.com/dp/{asin}"
    driver.get(url)
    time.sleep(1)
    handle_continue_pages(driver)

    # If amazon bot challenge appears, surface explicit error.
    page_src = (driver.page_source or "").lower()
    if "enter the characters you see below" in page_src:
        raise RuntimeError("Amazon anti-bot challenge detected")

    rating_text = find_text(
        driver,
        [
            (By.CSS_SELECTOR, "span[data-hook='rating-out-of-text']"),
            (By.CSS_SELECTOR, "#acrPopover .a-size-base.a-color-base"),
            (By.CSS_SELECTOR, "#acrPopover .a-icon-alt"),
        ],
    )
    review_text = find_text(
        driver,
        [
            (By.ID, "acrCustomerReviewText"),
            (By.CSS_SELECTOR, "a[data-hook='see-all-reviews-link-foot']"),
        ],
    )

    return {
        "asin": asin,
        "rating_raw": rating_text,
        "rating_value": parse_first_number(rating_text),
        "reviews_raw": review_text,
    }


def scrape_with_retry(driver: webdriver.Chrome, asin: str) -> dict:
    last_error = None
    for attempt in range(RETRIES_PER_ASIN):
        try:
            return scrape_rating_and_reviews(driver, asin)
        except Exception as e:
            last_error = str(e)
            time.sleep(1.5 * (attempt + 1))
    return {
        "asin": asin,
        "rating_raw": "未找到",
        "rating_value": "未找到",
        "reviews_raw": "未找到",
        "error": last_error or "unknown error",
    }


def main():
    driver = build_driver()
    try:
        zipcode = os.getenv("AMAZON_ZIPCODE", "10001")
        set_amazon_zipcode(driver, zipcode=zipcode)

        items = []
        for name, asin in PRODUCTS.items():
            result = scrape_with_retry(driver, asin)
            items.append({"name": name, **result})

        output = {
            "timestamp": datetime.now().isoformat(),
            "zipcode": zipcode,
            "items": items,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
