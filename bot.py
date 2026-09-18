#!/usr/bin/env python3
"""CODM Canadian Daily Gift bot - one-shot, headless, standalone."""

import os
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

STORE_URL = "https://store.callofdutymobile.com/en-ca/codm"
DAILY_GIFT_HEADING = "DAILY GIFT"
CLAIM_GIFT_TEXT = "CLAIM GIFT"
CLAIMED_TEXT = "Claimed"
SUCCESS_TEXT = "GIFT CLAIMED"


def load_uid() -> str:
    load_dotenv()
    uid = os.getenv("CODM_UID", "").strip()
    if not uid:
        print("Add UID UWU")
        sys.exit(1)
    return uid


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


def main() -> None:
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
                print("Daily Gift already claimed.")
                print("PASS check inboxie UWU")
                return

            print("Claiming Daily Gift...")
            claim_el = card.get_by_text(CLAIM_GIFT_TEXT, exact=True)
            if claim_el.count() == 0:
                print("Claim Gift button could not be found.")
                sys.exit(1)
            claim_el.first.scroll_into_view_if_needed()
            claim_el.first.click(force=True)

            dialog = page.locator("[role='dialog']").first
            dialog.wait_for(state="visible", timeout=10000)

            claim_btn = dialog.locator("[data-testid='claim-button']")
            if claim_btn.count() == 0 or not claim_btn.first.is_visible():
                print("Claim failed.")
                sys.exit(1)
            claim_btn.first.click()

            claim_btn.wait_for(state="hidden", timeout=15000)

            dialog_text = dialog.inner_text()
            if SUCCESS_TEXT in dialog_text:
                print("Daily Gift claimed successfully.")
                print("PASS check inboxie UWU")
                return

            if CLAIMED_TEXT in dialog_text and "inbox" in dialog_text.lower():
                print("Daily Gift claimed successfully.")
                print("PASS check inboxie UWU")
                return

            print("Unable to verify claim result.")
            sys.exit(1)

        except Exception as exc:
            print(f"Unexpected store state: {type(exc).__name__}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
