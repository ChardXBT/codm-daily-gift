#!/usr/bin/env python3
"""CODM Canadian Daily Gift bot - one-shot, headless, standalone."""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
LOG_FILE = Path(__file__).resolve().parent / "run.log"
ENV_PATH = Path(__file__).resolve().parent / ".env"
LATCH_PATH = Path(__file__).resolve().parent / "artifacts" / "submit-latch.json"
LOCK_PATH = Path(__file__).resolve().parent / "artifacts" / "run.lock"
# Phrases the Supervisor completion detector treats as success. Anything
# else is a failed run (exit 1), including strings that merely lack "error".
SUCCESS_RESULTS = frozenset({"already claimed", "claimed successfully"})
# The Supervisor reads run.log with these substrings. A line that contains
# one is retried instead of latched as a code failure. They are copied from
# the job's transient_failure_patterns and must stay exact.
SUPERVISOR_TRANSIENT_MARKERS = (
    "TimeoutError",
    "net::ERR_INTERNET_DISCONNECTED",
    "Could not resolve host",
    "getaddrinfo failed",
)
# Attention words in the same detector. A log line that contains one latches
# the job as an account problem, so store and exception text must not carry
# them through (including inside tokens such as CERT_AUTHORITY or BLOCKED).
SUPERVISOR_ATTENTION_FRAGMENTS = (
    "signin",
    "sign-in",
    "captcha",
    "challenge",
    "auth",
    "session_expired",
    "account_restricted",
    "blocked",
)
PRE_SUBMIT_STAGES = ("store", "player_id", "daily_gift_card", "claim_dialog")
POST_SUBMIT_STAGES = ("claim_submit", "claim_verification")
SUBMIT_ALREADY_RECORDED = (
    "error: claim already submitted today; result still unverified"
)
WITHHELD_STORE_DETAIL = "store returned a detail withheld from the log"
CARD_VISIBLE_TIMEOUT_MS = 15000

# The store is slow and occasionally stalls on a single step. The log
# shows a TimeoutError at 02:46:40 and a clean "already claimed" 75
# seconds later, so a stall is worth one more look rather than a lost
# day: the job is once-daily and the next attempt is 24 hours away.
RUN_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 45


def toronto_day(moment: datetime) -> date:
    """America/Toronto calendar date, without the tzdata package.

    The Supervisor decides "already ran today" in America/Toronto. The
    desktop Python install has no tzdata, so ZoneInfo cannot load that
    zone. Eastern DST (second Sunday of March 07:00 UTC through the first
    Sunday of November 06:00 UTC) is applied directly. Midnight is not in
    the spring gap or the fall overlap, so the calendar date is unambiguous.
    """
    if moment.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    utc = moment.astimezone(timezone.utc)
    start_day = _nth_weekday(utc.year, 3, 6, 2)
    end_day = _nth_weekday(utc.year, 11, 6, 1)
    dst_start = datetime(
        start_day.year, start_day.month, start_day.day, 7, tzinfo=timezone.utc
    )
    dst_end = datetime(
        end_day.year, end_day.month, end_day.day, 6, tzinfo=timezone.utc
    )
    offset = timedelta(hours=-4 if dst_start <= utc < dst_end else -5)
    return (utc + offset).date()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def load_uid() -> str:
    # The player id lives in this repo's .env. override=True keeps an
    # inherited CODM_UID from pointing the claim at a different account.
    load_dotenv(ENV_PATH, override=True)
    return os.getenv("CODM_UID", "").strip()


def one_line(result: str) -> str:
    single = " ".join(str(result).split())
    return single or "error: empty result"


def log_result(result: str) -> None:
    single = one_line(result)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} - {single}\n"
    prefix = ""
    if LOG_FILE.exists() and LOG_FILE.stat().st_size:
        with open(LOG_FILE, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(prefix + line)
        handle.flush()
        os.fsync(handle.fileno())
    print(line.strip())


def _git(repo: Path, args: list[str], timeout: int, *, check: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # An unattended run must not open a credential prompt. A failed push
    # stays on disk and is retried the next time the log is unpublished.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _unpushed_log_only(repo: Path) -> bool:
    """True when every unpublished commit touches only run.log."""
    count = _git(
        repo, ["rev-list", "--count", "@{upstream}..HEAD"], 10, check=False
    )
    if count.returncode != 0 or count.stdout.strip() in {"", "0"}:
        return False
    names = _git(
        repo, ["diff", "--name-only", "@{upstream}..HEAD"], 10, check=False
    )
    if names.returncode != 0:
        return False
    files = [line.strip().replace("\\", "/") for line in names.stdout.splitlines()]
    files = [line for line in files if line]
    return files == [LOG_FILE.name]


def git_push_log() -> None:
    """Commit and push run.log only.

    `git commit` with no pathspec would publish whatever else is staged,
    including a force-added env file. The commit is limited to the log,
    and it is made from the repo that owns the log even if the process
    was started elsewhere. The push is skipped when any unpublished
    commit touches another file, because that push would publish it too.
    """
    repo = LOG_FILE.resolve().parent
    try:
        diff = _git(
            repo,
            ["diff", "--quiet", "HEAD", "--", LOG_FILE.name],
            10,
            check=False,
        )
        if diff.returncode == 1:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            _git(
                repo,
                ["commit", "--only", "-m", f"log: {ts}", "--", LOG_FILE.name],
                10,
                check=True,
            )
        elif diff.returncode != 0:
            return
        # The log commit may sit on an unpublished code commit. Pushing
        # then would publish that code. Publish only a run.log history.
        if _unpushed_log_only(repo):
            _git(repo, ["push"], 30, check=True)
    except Exception:
        pass


def _pid_alive(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError as exc:
        # Access denied means some process owns the pid. Treat it as live
        # so a lock is never stolen from a process we cannot inspect.
        if getattr(exc, "winerror", None) == 5:
            return True
        return False
    else:
        return True


class RunLock:
    """One claim process at a time. A live holder makes the new process exit 75.

    Exit 75 is the Supervisor's resource-busy signal: the attempt is not
    counted and the job is not latched, because the holder will write the
    log. A lock whose pid is gone is stolen so a crash cannot block the
    next day.
    """

    def __init__(self, path: Path):
        self.path = path
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                if not self._stale():
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            try:
                os.write(
                    fd,
                    json.dumps({"pid": os.getpid()}).encode("utf-8"),
                )
                os.fsync(fd)
            finally:
                os.close(fd)
            self._held = True
            return True
        return False

    def _stale(self) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            # The holder creates the file before the pid write is visible.
            # An empty or half-written lock is live until it is a few
            # seconds old; a crash leaves it stealable after that.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return True
            return age > 5
        if not isinstance(raw, dict):
            return True
        return not _pid_alive(raw.get("pid"))

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("pid") != os.getpid():
            return
        try:
            self.path.unlink()
        except OSError:
            pass


def submit_blocked(path: Path | None, now: datetime | None = None) -> bool:
    """True when a claim click was already armed for this Toronto day.

    A missing file does not block. A corrupt file blocks: the click may
    already have been sent and the record of that is unreadable.
    """
    if path is None or not path.exists():
        return False
    now = now or datetime.now(timezone.utc)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return True
        stamp = datetime.fromisoformat(str(raw["submitted_at"]))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return True
    if stamp.tzinfo is None:
        return True
    try:
        return toronto_day(stamp) == toronto_day(now)
    except ValueError:
        return True


def record_submit(path: Path, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"submitted_at": now.isoformat()}).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_markers(exc: BaseException) -> list[str]:
    blob = f"{type(exc).__name__}\n{exc}"
    return [marker for marker in SUPERVISOR_TRANSIENT_MARKERS if marker in blob]


def _looks_like_transport(exc: BaseException) -> bool:
    if type(exc).__name__ in {"TimeoutError", "TargetClosedError"}:
        return True
    blob = f"{type(exc).__name__} {exc}"
    needles = (
        "net::ERR_",
        "getaddrinfo",
        "Could not resolve host",
        "ECONNRESET",
        "ECONNREFUSED",
        "ETIMEDOUT",
        "ENETUNREACH",
        "EAI_AGAIN",
        "Timeout ",
        "TargetClosed",
        "has been closed",
        "NS_ERROR_",
    )
    return any(needle in blob for needle in needles)


def failure_line(exc: BaseException, stage: str, uid: str = "") -> str:
    """One log-safe failure line.

    After the claim click, the line must not contain TimeoutError or any
    other Supervisor transient marker. Those markers make the Supervisor
    launch another process, and that process would click Claim again.
    The raw exception text is never copied: Playwright repeats the value
    that was just filled, and store errors can embed attention words.
    """
    name = type(exc).__name__
    markers = _safe_markers(exc)
    if stage in POST_SUBMIT_STAGES:
        markers = []
        if name == "TimeoutError":
            name = "ClaimTimeout"
    else:
        markers = [marker for marker in markers if marker != name]
    parts = [f"error: {name} at {stage}", *markers]
    if (
        stage in PRE_SUBMIT_STAGES
        and _looks_like_transport(exc)
        and name != "TimeoutError"
        and not markers
    ):
        parts.append("transport")
    line = " ".join(parts)
    if uid and len(uid) >= 6 and uid in line:
        line = line.replace(uid, "[redacted]")
    return one_line(line)


def _attention_or_contract_fragment(text: str) -> bool:
    lowered = text.lower()
    if any(fragment in lowered for fragment in SUPERVISOR_ATTENTION_FRAGMENTS):
        return True
    if any(marker.lower() in lowered for marker in SUPERVISOR_TRANSIENT_MARKERS):
        return True
    contract_phrases = (
        "claimed successfully",
        "already claimed",
        "add uid uwu",
        "traceback",
        "error:",
    )
    return any(phrase in lowered for phrase in contract_phrases)


def logged_store_error(text: str, uid: str) -> str:
    collapsed = " ".join(str(text).split())
    if uid and len(uid) >= 6:
        collapsed = collapsed.replace(uid, "[redacted]")
    if not collapsed or _attention_or_contract_fragment(collapsed):
        return WITHHELD_STORE_DETAIL
    return collapsed[:180]


def is_success(result: str) -> bool:
    return one_line(result) in SUCCESS_RESULTS


def wait_for_validation(page):
    page.get_by_text("MP Rank:", exact=False).wait_for(
        state="visible", timeout=45000
    )


def _visible_cards(cards) -> list:
    visible = []
    for index in range(cards.count()):
        item = cards.nth(index)
        try:
            shown = item.is_visible()
        except Exception:
            shown = False
        if shown:
            visible.append(item)
    return visible


def find_daily_gift_card(page):
    cards = page.get_by_text(DAILY_GIFT_HEADING, exact=True).locator(
        "xpath=ancestor::*[contains(@data-testid, 'sku-item-inner-container')][1]"
    )
    deadline = time.monotonic() + CARD_VISIBLE_TIMEOUT_MS / 1000
    while True:
        visible = _visible_cards(cards)
        if len(visible) == 1:
            return visible[0]
        if len(visible) > 1:
            raise LookupError(
                f"expected one Daily Gift card, saw {len(visible)}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("Daily Gift card not visible")
        page.wait_for_timeout(250)


def is_claimed(card) -> bool:
    overlay = card.locator("[data-testid='purchased-overlay']")
    if overlay.count() > 0 and overlay.first.is_visible():
        return True
    text = card.inner_text()
    return CLAIMED_TEXT in text and CLAIM_GIFT_TEXT not in text


def is_transient(result: str) -> bool:
    """Retry only stalls that happened before the final Claim request."""
    if not str(result).startswith("error:"):
        return False
    for stage in PRE_SUBMIT_STAGES:
        if f" at {stage}" not in result:
            continue
        if result.startswith(f"error: TimeoutError at {stage}"):
            return True
        if result.endswith(" transport"):
            return True
        for marker in SUPERVISOR_TRANSIENT_MARKERS:
            if marker != "TimeoutError" and marker in result:
                return True
    return False


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


def claim_flow(
    page,
    uid: str,
    stage_ref: list[str],
    latch_path: Path | None = None,
) -> str:
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
    try:
        card = find_daily_gift_card(page)
    except LookupError as exc:
        return f"error: {exc}"
    card.wait_for(state="visible", timeout=15000)

    if wait_for_claim_state(page, card):
        return "already claimed"

    # A previous process armed the claim click and then died. Do not click
    # the card or the confirm button again today; the card settle above is
    # the only proof that is still safe to trust.
    if submit_blocked(latch_path):
        return SUBMIT_ALREADY_RECORDED

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
    if latch_path is not None:
        try:
            record_submit(latch_path)
        except OSError:
            return "error: could not record claim submit"
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
            return f"error: store rejected claim: {logged_store_error(error, uid)}"
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
    if not uid:
        return "Add UID UWU"
    stage_ref = ["startup"]
    print("Starting CODM Daily Gift bot...")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                stage_ref[0] = "store"
                print("Opening store...")
                page.goto(
                    STORE_URL, wait_until="domcontentloaded", timeout=60000
                )
                return claim_flow(
                    page, uid, stage_ref, latch_path=LATCH_PATH
                )
            finally:
                # A close failure must not replace a confirmed claim result
                # or the exception that says which step stalled.
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        # Name the step. "error: TimeoutError" alone never said
        # whether the store, the ID field, the card or the claim
        # dialog was the slow one. After the claim click the type
        # name TimeoutError is withheld so the Supervisor does not
        # treat the line as a transient and launch another claim.
        return failure_line(exc, stage_ref[0], uid=uid)


def main() -> None:
    lock = RunLock(LOCK_PATH)
    if not lock.acquire():
        # No run.log line: an "error:" line would repair-latch the job
        # while the holder is still claiming.
        print("another claim run is active")
        raise SystemExit(75)
    code = 0
    try:
        if not load_uid():
            print("Add UID UWU")
            log_result("Add UID UWU")
            git_push_log()
            code = 1
        else:
            try:
                result = run_with_retries()
            except Exception as exc:
                result = failure_line(exc, "startup")
            visible = one_line(result)
            if is_success(visible):
                print("PASS check inboxie UWU")
            else:
                code = 1
            log_result(visible)
            git_push_log()
    finally:
        lock.release()
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
