#!/usr/bin/env python3
"""CODM Canadian Daily Gift bot - one-shot, headless, standalone."""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

STORE_URL = "https://store.callofdutymobile.com/en-ca/codm"
DAILY_GIFT_HEADING = "DAILY GIFT"
CLAIM_GIFT_TEXT = "CLAIM GIFT"
CLAIMED_TEXT = "Claimed"
SUCCESS_TEXT = "GIFT CLAIMED"
# The claim dialog is a ".sheet-dialog.freebie-redeem-modal" panel. Its
# [role='dialog'] wrapper has no box of its own, so Playwright never sees
# it as visible -- waiting on that is what made every claim time out.
CLAIM_DIALOG = ".freebie-redeem-modal:has([data-testid='freebie-redeem-modal'])"
# The card renders "CLAIM GIFT" before the store hydrates the player's
# claim state; the Claimed overlay can arrive a second or more later.
CLAIM_STATE_SETTLE_MS = 8000
LOG_FILE = Path(__file__).parent / "run.log"

# The store is slow and occasionally stalls on a single step. The log
# shows a TimeoutError at 02:46:40 and a clean "already claimed" 75
# seconds later, so a stall is worth one more look rather than a lost
# day: the job is once-daily and the next attempt is 24 hours away.
RUN_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 45


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


def is_transient(result: str) -> bool:
    """Retry only stalls that happened before the final Claim request."""
    return any(
        result == f"error: TimeoutError at {stage}"
        for stage in ("store", "player_id", "daily_gift_card", "claim_dialog")
    )


def dialog_shows_claimed(dialog) -> bool:
    """The dialog titles itself "Gift Claimed" once the gift is redeemed,
    whether it was redeemed just now or on an earlier run."""
    try:
        text = dialog.inner_text(timeout=1000)
    except Exception:
        return False
    lowered = text.lower()
    return SUCCESS_TEXT.lower() in lowered or (
        CLAIMED_TEXT.lower() in lowered and "inbox" in lowered
    )


def claim_confirmed(card, dialog) -> bool:
    """Accept either the store's card state or its confirmation dialog."""
    try:
        if is_claimed(card):
            return True
    except Exception:
        pass
    return dialog_shows_claimed(dialog)


def dialog_error(dialog) -> str | None:
    prompt = dialog.locator("[data-testid='error-prompt']")
    try:
        if prompt.count() and prompt.first.is_visible():
            return prompt.first.inner_text(timeout=1000).strip() or "unknown"
    except Exception:
        pass
    return None


def wait_for_claim_state(page, card) -> bool:
    """Give the card time to show a claim the store already recorded."""
    deadline = time.monotonic() + CLAIM_STATE_SETTLE_MS / 1000
    while True:
        if is_claimed(card):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def claim_flow(page, uid: str, stage_ref: list[str]) -> str:
    """Everything after the store page has loaded. stage_ref[0] names the
    current step so a timeout can be reported (and retried) by stage."""
    stage_ref[0] = "player_id"
    print("Entering Player ID...")
    uid_field = page.locator("#userId")
    uid_field.wait_for(state="visible", timeout=30000)
    uid_field.click()
    uid_field.fill(uid)
    uid_field.press("Tab")

    wait_for_validation(page)

    stage_ref[0] = "daily_gift_card"
    print("Finding Daily Gift...")
    card = find_daily_gift_card(page)
    card.wait_for(state="visible", timeout=15000)

    if wait_for_claim_state(page, card):
        return "already claimed"

    stage_ref[0] = "claim_dialog"
    print("Opening claim dialog...")
    claim_el = card.get_by_text(CLAIM_GIFT_TEXT, exact=True)
    if claim_el.count() == 0:
        return "error: Claim Gift button not found"
    claim_el.first.scroll_into_view_if_needed()
    claim_el.first.click(force=True)

    dialog = page.locator(CLAIM_DIALOG).first
    dialog.wait_for(state="visible", timeout=15000)

    # The dialog opens either offering "CLAIM" or already titled
    # "Gift Claimed" (a claim the card had not rendered yet).
    claim_btn = dialog.locator("[data-testid='claim-button']")
    for _ in range(20):
        if dialog_shows_claimed(dialog):
            return "already claimed"
        if (claim_btn.count() and claim_btn.first.is_visible()
                and claim_btn.first.is_enabled()):
            break
        page.wait_for_timeout(500)
    else:
        # Nothing has been submitted yet, so this is safe to retry.
        raise TimeoutError("claim button never became clickable")

    # The click may reach the store even when Playwright times out.
    # From this point on, never submit again without external proof.
    stage_ref[0] = "claim_submit"
    print("Confirming claim...")
    claim_btn.first.click()
    stage_ref[0] = "claim_verification"
    for _ in range(30):
        if claim_confirmed(card, dialog):
            return "claimed successfully"
        error = dialog_error(dialog)
        if error:
            if "already claimed" in error.lower():
                return "already claimed"
            return f"error: store rejected claim: {error}"
        page.wait_for_timeout(1000)
    return "error: Claim result unverified after submit"


def run_with_retries() -> str:
    for attempt in range(RUN_ATTEMPTS):
        result = run()
        if "error" not in result or not is_transient(result):
            return result
        if attempt + 1 < RUN_ATTEMPTS:
            print(f"{result}; retrying in {RETRY_DELAY_SECONDS}s "
                  f"({attempt + 2}/{RUN_ATTEMPTS})")
            time.sleep(RETRY_DELAY_SECONDS)
    return result


def run() -> str:
    uid = load_uid()
    stage_ref = ["startup"]
    print("Starting CODM Daily Gift bot...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            stage_ref[0] = "store"
            print("Opening store...")
            page.goto(STORE_URL, wait_until="domcontentloaded", timeout=60000)

            return claim_flow(page, uid, stage_ref)

        except Exception as exc:
            # Name the step. "error: TimeoutError" alone never said
            # whether the store, the ID field, the card or the claim
            # dialog was the slow one.
            return f"error: {type(exc).__name__} at {stage_ref[0]}"
        finally:
            browser.close()


def main() -> None:
    result = run_with_retries()
    print("PASS check inboxie UWU" if "error" not in result else "")
    log_result(result)
    git_push_log()
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
