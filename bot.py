#!/usr/bin/env python3
"""CODM Canadian Daily Gift bot - one-shot, headless, standalone."""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

STORE_URL = "https://store.callofdutymobile.com/en-ca/codm"
DAILY_GIFT_HEADING = "DAILY GIFT"
CLAIM_GIFT_TEXT = "CLAIM GIFT"
CLAIMED_TEXT = "Claimed"
SUCCESS_TEXT = "GIFT CLAIMED"
LOG_FILE = Path(__file__).parent / "run.log"


def load_uid() -> str:
    load_dotenv()
    uid = os.getenv("CODM_UID", "").strip()
    if not uid:
        print("Add UID UWU")
        sys.exit(1)
    return uid


def log_result(result: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} - {result}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())


def git_push_log() -> None:
    try:
        subprocess.run(
            ["git", "add", str(LOG_FILE)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet", str(LOG_FILE)],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            ["git", "commit", "-m", f"log: {ts}"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "push"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def wait_for_validation(page):
    page.get_by_text("MP Rank:", exact=False).wait_for(
        state="visible", timeout=45000
    )


def find_daily_gift_card(page):
    heading = page.get_by_text(DAILY_GIFT_HEADING, exact=True)
    heading.wait_for(state="visible", timeout=15000)
    return heading.locator(
        "xpath=ancestor::*[contains(@data-testid, 'sku-item-inner-container')][1]"
    )


def is_claimed(card) -> bool:
    overlay = card.locator("[data-testid='purchased-overlay']")
    if overlay.count() > 0 and overlay.first.is_visible():
        return True
    text = card.inner_text()
    return CLAIMED_TEXT in text and CLAIM_GIFT_TEXT not in text


def run() -> str:
    uid = load_uid()
    print("Starting CODM Daily Gift bot...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            print("Opening store...")
            page.goto(STORE_URL, wait_until="domcontentloaded", timeout=60000)

            print("Entering Player ID...")
            uid_field = page.locator("#userId")
            uid_field.wait_for(state="visible", timeout=30000)
            uid_field.click()
            uid_field.fill(uid)
            uid_field.press("Tab")

            wait_for_validation(page)

            print("Finding Daily Gift...")
            card = find_daily_gift_card(page)
            card.wait_for(state="visible", timeout=15000)

            if is_claimed(card):
                return "already claimed"

            print("Claiming Daily Gift...")
            claim_el = card.get_by_text(CLAIM_GIFT_TEXT, exact=True)
            if claim_el.count() == 0:
                return "error: Claim Gift button not found"
            claim_el.first.scroll_into_view_if_needed()
            claim_el.first.click(force=True)

            dialog = page.locator("[role='dialog']").first
            dialog.wait_for(state="visible", timeout=10000)

            claim_btn = dialog.locator("[data-testid='claim-button']")
            if claim_btn.count() == 0 or not claim_btn.first.is_visible():
                return "error: Claim button not found in dialog"
            claim_btn.first.click()

            claim_btn.wait_for(state="hidden", timeout=15000)

            dialog_text = dialog.inner_text()
            if SUCCESS_TEXT in dialog_text:
                return "claimed successfully"
            if CLAIMED_TEXT in dialog_text and "inbox" in dialog_text.lower():
                return "claimed successfully"

            return "error: Unable to verify claim result"

        except Exception as exc:
            return f"error: {type(exc).__name__}"
        finally:
            browser.close()


def main() -> None:
    result = run()
    print("PASS check inboxie UWU" if "error" not in result else "")
    log_result(result)
    git_push_log()
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
