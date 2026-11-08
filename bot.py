#!/usr/bin/env python3
"""CODM Canadian Daily Gift bot - one-shot, headless, standalone."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum, IntEnum
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
# Must equal {CLAIMED_LINE, ALREADY_CLAIMED_LINE} below (checked at import).
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
CLAIMED_LINE = "claimed successfully"
ALREADY_CLAIMED_LINE = "already claimed"
CONFIG_MISSING_LINE = "Add UID UWU"
FAILURE_PREFIX = "error:"
if SUCCESS_RESULTS != {CLAIMED_LINE, ALREADY_CLAIMED_LINE}:
    raise RuntimeError("SUCCESS_RESULTS must be exactly the two success lines")
SUBMIT_ALREADY_RECORDED = (
    "error: claim already submitted today; result still unverified"
)
WITHHELD_STORE_DETAIL = "store returned a detail withheld from the log"
CARD_VISIBLE_TIMEOUT_MS = 15000


class ExitCode(IntEnum):
    """Process exit codes. Part of the Supervisor contract; values are fixed."""

    OK = 0  # a SUCCESS_RESULTS line was logged
    FAILED = 1  # any other logged line
    BUSY = 75  # another claim run holds the lock; nothing logged, attempt refunded


class SubmitPhase(Enum):
    """Where a stage sits relative to the one irreversible claim click."""

    STARTUP = "startup"  # no store page yet; nothing can have been claimed
    PRE_SUBMIT = "pre_submit"  # no claim click yet; a stall is safe to retry
    POST_SUBMIT = "post_submit"  # the click may have reached the store


class Stage(str, Enum):
    """The claim pipeline, in order. Every stage declares its SubmitPhase."""

    STARTUP = "startup"
    STORE = "store"
    PLAYER_ID = "player_id"
    DAILY_GIFT_CARD = "daily_gift_card"
    CLAIM_DIALOG = "claim_dialog"
    CLAIM_SUBMIT = "claim_submit"
    CLAIM_VERIFICATION = "claim_verification"
    # A stage name outside this vocabulary. It may be after the click, so
    # it is handled as post-submit: never retried, no retry token logged.
    UNKNOWN = "unknown"


STAGE_PHASES: dict[Stage, SubmitPhase] = {
    Stage.STARTUP: SubmitPhase.STARTUP,
    Stage.STORE: SubmitPhase.PRE_SUBMIT,
    Stage.PLAYER_ID: SubmitPhase.PRE_SUBMIT,
    Stage.DAILY_GIFT_CARD: SubmitPhase.PRE_SUBMIT,
    Stage.CLAIM_DIALOG: SubmitPhase.PRE_SUBMIT,
    Stage.CLAIM_SUBMIT: SubmitPhase.POST_SUBMIT,
    Stage.CLAIM_VERIFICATION: SubmitPhase.POST_SUBMIT,
    Stage.UNKNOWN: SubmitPhase.POST_SUBMIT,
}
if set(STAGE_PHASES) != set(Stage):
    raise RuntimeError("every Stage must declare its SubmitPhase")
# The only forward steps the pipeline takes. UNKNOWN is never a target.
PIPELINE_ORDER = (
    Stage.STARTUP,
    Stage.STORE,
    Stage.PLAYER_ID,
    Stage.DAILY_GIFT_CARD,
    Stage.CLAIM_DIALOG,
    Stage.CLAIM_SUBMIT,
    Stage.CLAIM_VERIFICATION,
)
# Legacy names for the two partitions, derived from STAGE_PHASES.
PRE_SUBMIT_STAGES = tuple(
    stage.value for stage in PIPELINE_ORDER
    if STAGE_PHASES[stage] is SubmitPhase.PRE_SUBMIT
)
POST_SUBMIT_STAGES = tuple(
    stage.value for stage in PIPELINE_ORDER
    if STAGE_PHASES[stage] is SubmitPhase.POST_SUBMIT
)


def parse_stage(value: object) -> Stage:
    """A Stage from a Stage or its name. Any other value is Stage.UNKNOWN."""
    if isinstance(value, Stage):
        return value
    try:
        return Stage(value)
    except ValueError:
        return Stage.UNKNOWN


def stage_phase(stage: Stage) -> SubmitPhase:
    if not isinstance(stage, Stage):
        raise TypeError(f"not a Stage: {stage!r}")
    return STAGE_PHASES[stage]


def advance_stage(stage_ref: list, target: Stage) -> None:
    """Move the pipeline exactly one step forward.

    A step back (in particular from after the claim click to before it)
    would make a post-submit failure look retryable, so it raises.
    """
    if target not in PIPELINE_ORDER:
        raise ValueError(f"not a pipeline stage: {target!r}")
    current = parse_stage(stage_ref[0])
    if current not in PIPELINE_ORDER:
        raise ValueError(f"cannot leave stage {current.value}")
    if PIPELINE_ORDER.index(target) != PIPELINE_ORDER.index(current) + 1:
        raise ValueError(
            f"stage {current.value} cannot move to {target.value}"
        )
    stage_ref[0] = target


def _has_transient_marker(text: str) -> bool:
    # The Supervisor compiles its patterns with re.IGNORECASE.
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in SUPERVISOR_TRANSIENT_MARKERS)


class ResultKind(Enum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"
    CONFIG_MISSING = "config_missing"
    FAILED = "failed"


# The exact log line of each fixed-text kind. FAILED lines vary.
RESULT_LINES = {
    ResultKind.CLAIMED: CLAIMED_LINE,
    ResultKind.ALREADY_CLAIMED: ALREADY_CLAIMED_LINE,
    ResultKind.CONFIG_MISSING: CONFIG_MISSING_LINE,
}


@dataclass(frozen=True)
class RunResult:
    """The outcome of one claim run.

    Retryability and the exit code are read from kind, stage and stalled,
    never parsed back out of the line. The line is the exact text written
    to run.log, which the Supervisor greps (see README, Supervisor contract).
    """

    kind: ResultKind
    line: str
    stage: Stage | None = None
    stalled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResultKind):
            raise TypeError(f"not a ResultKind: {self.kind!r}")
        if not isinstance(self.line, str) or self.line != one_line(self.line):
            raise ValueError("a result line is one non-empty line")
        if self.stage is not None and not isinstance(self.stage, Stage):
            raise TypeError(f"not a Stage: {self.stage!r}")
        if self.kind is ResultKind.FAILED:
            if not self.line.startswith(FAILURE_PREFIX):
                raise ValueError("a failure line starts with 'error:'")
            if self.stage is None:
                raise ValueError("a failure names its stage")
        elif self.kind in RESULT_LINES:
            if self.line != RESULT_LINES[self.kind]:
                raise ValueError(f"{self.kind.value} must log {RESULT_LINES[self.kind]!r}")
        else:
            raise ValueError(f"unhandled ResultKind {self.kind!r}")
        if self.stalled and (
            self.kind is not ResultKind.FAILED
            or stage_phase(self.stage) is not SubmitPhase.PRE_SUBMIT
        ):
            raise ValueError("only a pre-submit failure can be a retryable stall")
        if (
            self.stage is not None
            and stage_phase(self.stage) is SubmitPhase.POST_SUBMIT
            and _has_transient_marker(self.line)
        ):
            # The Supervisor would relaunch and the new process would
            # click Claim again.
            raise ValueError("a post-submit line must not carry a retry token")

    @classmethod
    def claimed(cls, stage: Stage | None = None) -> "RunResult":
        return cls(ResultKind.CLAIMED, CLAIMED_LINE, stage)

    @classmethod
    def already_claimed(cls, stage: Stage | None = None) -> "RunResult":
        return cls(ResultKind.ALREADY_CLAIMED, ALREADY_CLAIMED_LINE, stage)

    @classmethod
    def config_missing(cls) -> "RunResult":
        return cls(ResultKind.CONFIG_MISSING, CONFIG_MISSING_LINE)

    @classmethod
    def failed(cls, line: str, stage: Stage, *, stalled: bool = False) -> "RunResult":
        return cls(ResultKind.FAILED, one_line(line), stage, stalled)

    @property
    def succeeded(self) -> bool:
        if self.kind in (ResultKind.CLAIMED, ResultKind.ALREADY_CLAIMED):
            return True
        if self.kind in (ResultKind.CONFIG_MISSING, ResultKind.FAILED):
            return False
        raise ValueError(f"unhandled ResultKind {self.kind!r}")

    @property
    def retryable(self) -> bool:
        """Only a stall before the final Claim click is retried in-process."""
        return self.stalled

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.succeeded else ExitCode.FAILED

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
    step = "diff"
    try:
        diff = _git(
            repo,
            ["diff", "--quiet", "HEAD", "--", LOG_FILE.name],
            10,
            check=False,
        )
        if diff.returncode == 1:
            step = "commit"
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            _git(
                repo,
                ["commit", "--only", "-m", f"log: {ts}", "--", LOG_FILE.name],
                10,
                check=True,
            )
        elif diff.returncode != 0:
            print(f"run.log publish skipped: git diff exit {diff.returncode}")
            return
        # The log commit may sit on an unpublished code commit. Pushing
        # then would publish that code. Publish only a run.log history.
        step = "push"
        if _unpushed_log_only(repo):
            _git(repo, ["push"], 30, check=True)
    except Exception as exc:
        # stdout only, and never git's own text: it can name the remote.
        # A failed push stays local and is retried after the next result.
        code = getattr(exc, "returncode", None)
        suffix = f" exit {code}" if isinstance(code, int) else ""
        print(f"run.log publish failed at {step}: {type(exc).__name__}{suffix}")


# A claim run is bounded by its Playwright timeouts and retry sleeps (well
# under 20 minutes for all three attempts). A lock older than this whose
# holder cannot be proven to be the process that wrote it is abandoned.
LOCK_UNVERIFIED_MAX_AGE_SECONDS = 3600
# The holder creates the lock file before its pid write is visible. An
# unreadable lock younger than this is a holder mid-write.
LOCK_TORN_GRACE_SECONDS = 5


class ProcessState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"  # a process may exist that we cannot inspect


class LockState(Enum):
    """What an existing run.lock says about its holder."""

    TORN_FRESH = "torn_fresh"  # unreadable, younger than the grace period
    TORN_STALE = "torn_stale"  # unreadable and older (a crash mid-write)
    MALFORMED = "malformed"  # JSON that is not {"pid": positive int, ...}
    OWN_PID = "own_pid"  # this process's pid
    DEAD_HOLDER = "dead_holder"
    REUSED_PID = "reused_pid"  # live pid, recorded creation time differs
    LIVE_HOLDER = "live_holder"  # live pid, creation time matches
    UNVERIFIED_LIVE = "unverified_live"  # live pid we cannot tie to the writer
    UNVERIFIED_EXPIRED = "unverified_expired"  # same, but older than any real run


# Lock states a new run may delete and replace. Every other state means a
# claim run may still be active: exit 75 and log nothing.
LOCK_STEALABLE = frozenset({
    LockState.TORN_STALE,
    LockState.MALFORMED,
    LockState.DEAD_HOLDER,
    LockState.REUSED_PID,
    LockState.UNVERIFIED_EXPIRED,
})
LOCK_HELD = frozenset(LockState) - LOCK_STEALABLE


def _windows_process_identity(pid: int) -> tuple[ProcessState, int | None]:
    """("alive" | "dead" | "unknown", creation time in 100 ns units).

    os.kill(pid, 0) is not a liveness probe on Windows: signal 0 is
    CTRL_C_EVENT, and GenerateConsoleCtrlEvent "succeeds" for an exited
    process whose handle is still open and for any live process, including
    one that merely reused the pid.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        # 87: no process has this id. Anything else (5 = access denied)
        # means a process exists that we cannot inspect.
        if error == 87:
            return ProcessState.DEAD, None
        return ProcessState.UNKNOWN, None
    try:
        wait_timeout = 0x102
        if kernel32.WaitForSingleObject(handle, 0) != wait_timeout:
            return ProcessState.DEAD, None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ProcessState.ALIVE, None
        return ProcessState.ALIVE, (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _process_identity(pid: object) -> tuple[ProcessState, int | None]:
    if type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF:
        return ProcessState.DEAD, None
    if os.name == "nt":
        try:
            return _windows_process_identity(pid)
        except Exception:
            # ctypes failures are not OSError. Unknown keeps the lock until
            # it is old enough that no real claim run can still own it.
            return ProcessState.UNKNOWN, None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return ProcessState.UNKNOWN, None
    except OSError:
        return ProcessState.DEAD, None
    return ProcessState.ALIVE, None


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
                owner: dict[str, int] = {"pid": os.getpid()}
                created = _process_identity(os.getpid())[1]
                if created is not None:
                    owner["created"] = created
                os.write(fd, json.dumps(owner).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._held = True
            return True
        return False

    def _age_seconds(self) -> float | None:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return None

    def state(self) -> LockState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            # An empty or half-written lock is live until it is a few
            # seconds old; a crash leaves it stealable after that.
            age = self._age_seconds()
            if age is None or age > LOCK_TORN_GRACE_SECONDS:
                return LockState.TORN_STALE
            return LockState.TORN_FRESH
        if not isinstance(raw, dict):
            return LockState.MALFORMED
        pid = raw.get("pid")
        if type(pid) is not int or pid <= 0:
            return LockState.MALFORMED
        if pid == os.getpid():
            return LockState.OWN_PID
        process, created = _process_identity(pid)
        if process is ProcessState.DEAD:
            return LockState.DEAD_HOLDER
        recorded = raw.get("created")
        if (
            process is ProcessState.ALIVE
            and type(recorded) is int
            and created is not None
        ):
            # Same pid, different process: the holder died and Windows gave
            # its pid to someone else.
            if recorded != created:
                return LockState.REUSED_PID
            return LockState.LIVE_HOLDER
        if process not in (ProcessState.ALIVE, ProcessState.UNKNOWN):
            raise ValueError(f"unhandled ProcessState {process!r}")
        # A live pid we cannot tie to the writer (an older lock without a
        # creation time, or access denied). A real claim run is long over
        # once the lock is this old.
        age = self._age_seconds()
        if age is None or age > LOCK_UNVERIFIED_MAX_AGE_SECONDS:
            return LockState.UNVERIFIED_EXPIRED
        return LockState.UNVERIFIED_LIVE

    def _stale(self) -> bool:
        state = self.state()
        if state in LOCK_STEALABLE:
            return True
        if state in LOCK_HELD:
            return False
        raise ValueError(f"unhandled LockState {state!r}")

    def holder_summary(self) -> str:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            pid = raw.get("pid") if isinstance(raw, dict) else None
        except (OSError, UnicodeError, ValueError):
            pid = None
        age = self._age_seconds()
        age_text = "unknown" if age is None else f"{age:.0f}s"
        return f"lock holder pid={pid} age={age_text}"

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


class LatchState(Enum):
    """Whether a claim click was already armed for this Toronto day.

    The latch file holds only {"submitted_at": <tz-aware ISO time>}; its
    one writer (record_submit) only ever arms it, so the state is derived
    from the file and the clock rather than stored.
    """

    UNARMED = "unarmed"  # no file, or a stamp from an earlier Toronto day
    ARMED = "armed"  # a well-formed stamp from this Toronto day
    # Unreadable, legacy or future-dated: the click may have been sent and
    # the record of it cannot be trusted. Handled as armed (never re-claim).
    ARMED_UNKNOWN = "armed_unknown"


def latch_state(path: Path | None, now: datetime | None = None) -> LatchState:
    if path is None or not path.exists():
        return LatchState.UNARMED
    now = now or datetime.now(timezone.utc)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return LatchState.ARMED_UNKNOWN
        stamp = datetime.fromisoformat(str(raw["submitted_at"]))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return LatchState.ARMED_UNKNOWN
    if stamp.tzinfo is None:
        return LatchState.ARMED_UNKNOWN
    try:
        armed_day = toronto_day(stamp)
        today = toronto_day(now)
    except ValueError:
        return LatchState.ARMED_UNKNOWN
    if armed_day == today:
        return LatchState.ARMED
    if armed_day < today:
        return LatchState.UNARMED
    # Stamped on a later day than "now": a clock that was ahead or has
    # been set back. Which day the click belongs to is unknown.
    return LatchState.ARMED_UNKNOWN


def submit_blocked(path: Path | None, now: datetime | None = None) -> bool:
    """True when a claim click was already armed for this Toronto day.

    A missing file does not block. A corrupt file blocks: the click may
    already have been sent and the record of that is unreadable.
    """
    state = latch_state(path, now)
    if state is LatchState.UNARMED:
        return False
    if state in (LatchState.ARMED, LatchState.ARMED_UNKNOWN):
        return True
    raise ValueError(f"unhandled LatchState {state!r}")


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


def failure_line(exc: BaseException, stage: Stage | str, uid: str = "") -> RunResult:
    """One log-safe failure result.

    After the claim click, the line must not contain TimeoutError or any
    other Supervisor transient marker. Those markers make the Supervisor
    launch another process, and that process would click Claim again.
    The raw exception text is never copied: Playwright repeats the value
    that was just filled, and store errors can embed attention words.
    """
    stage = parse_stage(stage)
    phase = stage_phase(stage)
    name = type(exc).__name__
    markers = _safe_markers(exc)
    stalled = False
    transport = False
    if phase is SubmitPhase.POST_SUBMIT:
        markers = []
        # Any class name carrying a retry token (TimeoutError itself, or
        # e.g. a ConnectTimeoutError) is renamed, not only the exact one.
        if _has_transient_marker(name):
            name = "ClaimTimeout"
    elif phase in (SubmitPhase.PRE_SUBMIT, SubmitPhase.STARTUP):
        markers = [marker for marker in markers if marker != name]
        if phase is SubmitPhase.PRE_SUBMIT:
            transport = (
                _looks_like_transport(exc)
                and name != "TimeoutError"
                and not markers
            )
            stalled = (
                name == "TimeoutError"
                or transport
                or any(marker != "TimeoutError" for marker in markers)
            )
    else:
        raise ValueError(f"unhandled SubmitPhase {phase!r}")
    parts = [f"error: {name} at {stage.value}", *markers]
    if transport:
        parts.append("transport")
    line = " ".join(parts)
    if uid and len(uid) >= 6 and uid in line:
        line = line.replace(uid, "[redacted]")
    return RunResult.failed(line, stage, stalled=stalled)


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


def is_success(result: RunResult) -> bool:
    if not isinstance(result, RunResult):
        raise TypeError(f"not a RunResult: {type(result).__name__}")
    return result.succeeded


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


def is_transient(result: RunResult) -> bool:
    """Retry only stalls that happened before the final Claim request.

    Read from the structured result. A bare log string is refused: parsing
    retryability back out of text is how a post-submit line that happened
    to contain " at store" or " transport" could be retried.
    """
    if not isinstance(result, RunResult):
        raise TypeError(f"not a RunResult: {type(result).__name__}")
    return result.retryable


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
    stage_ref: list,
    latch_path: Path | None = None,
) -> RunResult:
    """Everything after the store page has loaded. stage_ref[0] names the
    current Stage so a timeout can be reported (and retried) by stage."""
    advance_stage(stage_ref, Stage.PLAYER_ID)
    print("Entering Player ID...")
    uid_field = page.locator("#userId")
    uid_field.wait_for(state="visible", timeout=30000)
    uid_field.click()
    uid_field.fill(uid)
    uid_field.press("Tab")

    wait_for_validation(page)

    advance_stage(stage_ref, Stage.DAILY_GIFT_CARD)
    print("Finding Daily Gift...")
    try:
        card = find_daily_gift_card(page)
    except LookupError as exc:
        return RunResult.failed(f"error: {exc}", Stage.DAILY_GIFT_CARD)
    card.wait_for(state="visible", timeout=15000)

    if wait_for_claim_state(page, card):
        return RunResult.already_claimed(Stage.DAILY_GIFT_CARD)

    # A previous process armed the claim click and then died. Do not click
    # the card or the confirm button again today; the card settle above is
    # the only proof that is still safe to trust.
    if submit_blocked(latch_path):
        return RunResult.failed(SUBMIT_ALREADY_RECORDED, Stage.DAILY_GIFT_CARD)

    advance_stage(stage_ref, Stage.CLAIM_DIALOG)
    print("Opening claim dialog...")
    claim_el = card.get_by_text(CLAIM_GIFT_TEXT, exact=True)
    if claim_el.count() == 0:
        return RunResult.failed(
            "error: Claim Gift button not found", Stage.CLAIM_DIALOG
        )
    claim_el.first.scroll_into_view_if_needed()
    claim_el.first.click(force=True)

    dialog = page.locator(CLAIM_DIALOG).first
    dialog.wait_for(state="visible", timeout=15000)

    # The dialog opens either offering "CLAIM" or already titled
    # "Gift Claimed" (a claim the card had not rendered yet).
    claim_btn = dialog.locator("[data-testid='claim-button']")
    for _ in range(20):
        if dialog_shows_claimed(dialog):
            return RunResult.already_claimed(Stage.CLAIM_DIALOG)
        if (claim_btn.count() and claim_btn.first.is_visible()
                and claim_btn.first.is_enabled()):
            break
        page.wait_for_timeout(500)
    else:
        # Nothing has been submitted yet, so this is safe to retry.
        raise TimeoutError("claim button never became clickable")

    # The click may reach the store even when Playwright times out.
    # From this point on, never submit again without external proof.
    advance_stage(stage_ref, Stage.CLAIM_SUBMIT)
    if latch_path is not None:
        try:
            record_submit(latch_path)
        except OSError:
            return RunResult.failed(
                "error: could not record claim submit", Stage.CLAIM_SUBMIT
            )
    print("Confirming claim...")
    claim_btn.first.click()
    advance_stage(stage_ref, Stage.CLAIM_VERIFICATION)
    for _ in range(30):
        if claim_confirmed(card, dialog):
            return RunResult.claimed(Stage.CLAIM_VERIFICATION)
        error = dialog_error(dialog)
        if error:
            if "already claimed" in error.lower():
                return RunResult.already_claimed(Stage.CLAIM_VERIFICATION)
            return RunResult.failed(
                f"error: store rejected claim: {logged_store_error(error, uid)}",
                Stage.CLAIM_VERIFICATION,
            )
        page.wait_for_timeout(1000)
    return RunResult.failed(
        "error: Claim result unverified after submit", Stage.CLAIM_VERIFICATION
    )


def run_with_retries() -> RunResult:
    for attempt in range(RUN_ATTEMPTS):
        result = run()
        if not is_transient(result):
            return result
        if attempt + 1 < RUN_ATTEMPTS:
            print(f"{result.line}; retrying in {RETRY_DELAY_SECONDS}s "
                  f"({attempt + 2}/{RUN_ATTEMPTS})")
            time.sleep(RETRY_DELAY_SECONDS)
    return result


def run() -> RunResult:
    uid = load_uid()
    if not uid:
        return RunResult.config_missing()
    stage_ref: list = [Stage.STARTUP]
    print("Starting CODM Daily Gift bot...")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                advance_stage(stage_ref, Stage.STORE)
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


def startup_line() -> str:
    """stdout only: run.log is the Supervisor's result file and stays one
    result per line."""
    try:
        head = _git(
            LOG_FILE.resolve().parent, ["rev-parse", "HEAD"], 10, check=False
        )
        sha = head.stdout.strip()[:12] if head.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return (
        f"codm-daily-gift start: git={sha or 'unknown'} pid={os.getpid()} "
        f"headless=True attempts={RUN_ATTEMPTS} "
        f"retry_delay={RETRY_DELAY_SECONDS}s"
    )


def main() -> None:
    print(startup_line())
    lock = RunLock(LOCK_PATH)
    if not lock.acquire():
        # No run.log line: an "error:" line would repair-latch the job
        # while the holder is still claiming.
        print("another claim run is active")
        print(lock.holder_summary())
        raise SystemExit(int(ExitCode.BUSY))
    code = ExitCode.OK
    try:
        if not load_uid():
            result = RunResult.config_missing()
            print(result.line)
        else:
            try:
                result = run_with_retries()
                if not isinstance(result, RunResult):
                    raise TypeError(f"not a RunResult: {type(result).__name__}")
            except Exception as exc:
                result = failure_line(exc, Stage.STARTUP)
            if is_success(result):
                print("PASS check inboxie UWU")
        code = result.exit_code
        log_result(result.line)
        git_push_log()
    finally:
        lock.release()
    if code is not ExitCode.OK:
        raise SystemExit(int(code))


if __name__ == "__main__":
    main()
