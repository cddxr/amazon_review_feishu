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


def build_driver() -> webdriver.Chrome:
    """
    Read local browser config from env vars:
    - CHROMEDRIVER_PATH (required)
    - CHROME_BINARY (optional)
    """
    chrome_driver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if not chrome_driver_path:
        raise RuntimeError("Missing env CHROMEDRIVER_PATH")

    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary

    return webdriver.Chrome(service=Service(chrome_driver_path), options=options)


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


def build_report_line(name: str, info: dict) -> str:
    return (
        f"{name} ({info['asin']}): "
        f"rating={info['rating_value']}, reviews={info['reviews_raw']}"
    )


def main():
    driver = build_driver()
    try:
        zipcode = os.getenv("AMAZON_ZIPCODE", "10001")
        set_amazon_zipcode(driver, zipcode=zipcode)

        results = []
        lines = [f"Amazon rating snapshot @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
        for name, asin in PRODUCTS.items():
            info = scrape_rating_and_reviews(driver, asin)
            results.append({"name": name, **info})
            lines.append(build_report_line(name, info))

        output = {
            "timestamp": datetime.now().isoformat(),
            "zipcode": zipcode,
            "items": results,
            "text_summary": "\n".join(lines),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
