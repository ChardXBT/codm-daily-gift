"""Declared states for the run result, pipeline stages, submit latch and run lock.

Each invalid-but-representable combination from IMPLICIT-STATE-MACHINES.md
is either impossible to build or explicitly guarded here. The Supervisor
contract (the run.log strings it greps and the exit codes) is pinned
byte-for-byte. These tests never open a browser or the store.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
from bot import (  # noqa: E402
    ExitCode,
    LatchState,
    LockState,
    ResultKind,
    RunResult,
    Stage,
    SubmitPhase,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The codm_daily_gift completion detector in bot-supervisor/config.yaml and
# the default attention words in bot_supervisor/daily_jobs.py, copied
# verbatim. The Supervisor compiles each with re.IGNORECASE and classifies a
# line as transient, then attention, then failed, then completed.
SUPERVISOR_SUCCESS_PATTERNS = ("already claimed", "claimed successfully")
SUPERVISOR_FAILURE_PATTERNS = (
    "error:",
    "Add UID UWU",
    r"Traceback \(most recent call last\):",
)
SUPERVISOR_TRANSIENT_PATTERNS = (
    "Could not resolve host",
    "getaddrinfo failed",
    "net::ERR_INTERNET_DISCONNECTED",
    "TimeoutError",
)
SUPERVISOR_ATTENTION_WORDS = (
    "signin",
    "sign-in",
    "captcha",
    "challenge",
    "auth",
    "session_expired",
    "account_restricted",
    "blocked",
)


def supervisor_classification(line: str) -> str:
    def hit(patterns):
        return any(re.search(p, line, re.IGNORECASE) for p in patterns)

    if hit(SUPERVISOR_TRANSIENT_PATTERNS):
        return "transient_failed"
    if hit(SUPERVISOR_ATTENTION_WORDS):
        return "attention"
    if hit(SUPERVISOR_FAILURE_PATTERNS):
        return "failed"
    if hit(SUPERVISOR_SUCCESS_PATTERNS):
        return "completed"
    return ""


class ConnectTimeoutError(Exception):
    pass


class SupervisorContractTests(unittest.TestCase):
    """Strings and exit codes the Supervisor reads. Change none of them."""

    def test_logged_strings_are_byte_identical(self):
        self.assertEqual(bot.SUCCESS_RESULTS, frozenset(SUPERVISOR_SUCCESS_PATTERNS))
        self.assertEqual(bot.CLAIMED_LINE, "claimed successfully")
        self.assertEqual(bot.ALREADY_CLAIMED_LINE, "already claimed")
        self.assertEqual(bot.CONFIG_MISSING_LINE, "Add UID UWU")
        self.assertEqual(bot.FAILURE_PREFIX, "error:")
        self.assertEqual(
            bot.SUBMIT_ALREADY_RECORDED,
            "error: claim already submitted today; result still unverified",
        )
        self.assertEqual(
            bot.SUPERVISOR_TRANSIENT_MARKERS,
            (
                "TimeoutError",
                "net::ERR_INTERNET_DISCONNECTED",
                "Could not resolve host",
                "getaddrinfo failed",
            ),
        )
        self.assertEqual(set(bot.SUPERVISOR_TRANSIENT_MARKERS), set(SUPERVISOR_TRANSIENT_PATTERNS))
        self.assertEqual(bot.SUPERVISOR_ATTENTION_FRAGMENTS, SUPERVISOR_ATTENTION_WORDS)

    def test_exit_codes_are_fixed(self):
        self.assertEqual(
            {code.name: int(code) for code in ExitCode},
            {"OK": 0, "FAILED": 1, "BUSY": 75},
        )
        self.assertEqual(RunResult.claimed().exit_code, 0)
        self.assertEqual(RunResult.already_claimed().exit_code, 0)
        self.assertEqual(RunResult.config_missing().exit_code, 1)
        self.assertEqual(
            RunResult.failed("error: x", Stage.STORE).exit_code, 1
        )

    def test_every_result_kind_classifies_as_before(self):
        cases = [
            (RunResult.claimed(), "completed"),
            (RunResult.already_claimed(), "completed"),
            (RunResult.config_missing(), "failed"),
            (RunResult.failed(bot.SUBMIT_ALREADY_RECORDED, Stage.DAILY_GIFT_CARD), "failed"),
            (bot.failure_line(TimeoutError("t"), "store"), "transient_failed"),
            (bot.failure_line(Exception("net::ERR_INTERNET_DISCONNECTED"), "player_id"),
             "transient_failed"),
        ]
        for result, expected in cases:
            with self.subTest(line=result.line):
                self.assertEqual(supervisor_classification(result.line), expected)

    def test_no_post_submit_failure_reaches_the_supervisor_as_transient(self):
        errors = [
            TimeoutError("Timeout 30000ms exceeded"),
            ConnectTimeoutError("x"),
            Exception("net::ERR_INTERNET_DISCONNECTED getaddrinfo failed"),
            Exception("Could not resolve host store"),
        ]
        for stage in (Stage.CLAIM_SUBMIT, Stage.CLAIM_VERIFICATION, "claim_inbox"):
            for exc in errors:
                with self.subTest(stage=stage, exc=type(exc).__name__):
                    result = bot.failure_line(exc, stage)
                    self.assertNotEqual(
                        supervisor_classification(result.line), "transient_failed"
                    )
                    self.assertFalse(result.retryable)

    @unittest.skipUnless(
        os.environ.get("CODM_SUPERVISOR_ROOT"),
        "set CODM_SUPERVISOR_ROOT to a bot-supervisor checkout to cross-check",
    )
    def test_pinned_patterns_match_the_supervisor_checkout(self):
        root = Path(os.environ["CODM_SUPERVISOR_ROOT"])
        lines = (root / "config.yaml").read_text(encoding="utf-8").splitlines()
        start = next(
            index for index, line in enumerate(lines)
            if line.strip() == "codm_daily_gift:"
            and index + 1 < len(lines) and "display_name" in lines[index + 1]
        )
        lists: dict[str, list[str]] = {}
        current = None
        for line in lines[start + 1:]:
            if line.strip() and not line.startswith("      "):
                break
            key = re.match(r"^\s+(\w+):\s*$", line)
            if key:
                current = key.group(1)
                lists[current] = []
                continue
            item = re.match(r'^\s+- (".*")\s*$', line)
            if item and current:
                lists[current].append(json.loads(item.group(1)))
        self.assertEqual(tuple(lists["success_patterns"]), SUPERVISOR_SUCCESS_PATTERNS)
        self.assertEqual(tuple(lists["failure_patterns"]), SUPERVISOR_FAILURE_PATTERNS)
        self.assertEqual(
            tuple(lists["transient_failure_patterns"]), SUPERVISOR_TRANSIENT_PATTERNS
        )
        self.assertNotIn("attention_patterns", lists)
        jobs = (root / "bot_supervisor" / "daily_jobs.py").read_text(encoding="utf-8")
        words = re.search(r"ATTENTION_WORDS = \((.*?)\)", jobs, re.DOTALL).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', words)), SUPERVISOR_ATTENTION_WORDS)


class RunResultTests(unittest.TestCase):
    def test_a_success_kind_carries_only_its_exact_phrase(self):
        for kind in (ResultKind.CLAIMED, ResultKind.ALREADY_CLAIMED, ResultKind.CONFIG_MISSING):
            for line in ("GIFT CLAIMED", "claimed", "already claimed ", "error: x"):
                with self.subTest(kind=kind, line=line):
                    with self.assertRaises(ValueError):
                        RunResult(kind, line)

    def test_a_failure_starts_with_error_and_names_its_stage(self):
        with self.assertRaises(ValueError):
            RunResult(ResultKind.FAILED, "claimed successfully", Stage.STORE)
        with self.assertRaises(ValueError):
            RunResult(ResultKind.FAILED, "error: x")

    def test_only_a_pre_submit_failure_can_be_a_retryable_stall(self):
        with self.assertRaises(ValueError):
            RunResult(ResultKind.CLAIMED, "claimed successfully", Stage.STORE, stalled=True)
        for stage in (Stage.STARTUP, Stage.CLAIM_SUBMIT, Stage.CLAIM_VERIFICATION, Stage.UNKNOWN):
            with self.subTest(stage=stage):
                with self.assertRaises(ValueError):
                    RunResult.failed("error: Exception at x", stage, stalled=True)
        self.assertTrue(RunResult.failed("error: x", Stage.STORE, stalled=True).retryable)

    def test_a_post_submit_line_cannot_carry_a_retry_token(self):
        for stage in (Stage.CLAIM_SUBMIT, Stage.CLAIM_VERIFICATION, Stage.UNKNOWN):
            for marker in bot.SUPERVISOR_TRANSIENT_MARKERS:
                for text in (marker, marker.lower(), marker.upper()):
                    with self.subTest(stage=stage, text=text):
                        with self.assertRaises(ValueError):
                            RunResult.failed(f"error: store rejected claim: {text}", stage)

    def test_a_result_line_is_one_line(self):
        with self.assertRaises(ValueError):
            RunResult(ResultKind.FAILED, "error: a\nb", Stage.STORE)
        with self.assertRaises(ValueError):
            RunResult(ResultKind.FAILED, "", Stage.STORE)

    def test_kind_and_stage_must_be_declared_values(self):
        with self.assertRaises(TypeError):
            RunResult("claimed", "claimed successfully")
        with self.assertRaises(TypeError):
            RunResult(ResultKind.FAILED, "error: x", "store")

    def test_every_kind_has_a_success_verdict(self):
        for kind in ResultKind:
            line = bot.RESULT_LINES.get(kind, "error: x")
            result = RunResult(kind, line, None if kind in bot.RESULT_LINES else Stage.STORE)
            self.assertIsInstance(result.succeeded, bool)

    def test_post_submit_timeout_subclass_name_is_scrubbed(self):
        # Regression: "error: ConnectTimeoutError at claim_submit" contained
        # the Supervisor's TimeoutError token, so it relaunched the claim.
        result = bot.failure_line(ConnectTimeoutError("x"), "claim_submit")
        self.assertEqual(result.line, "error: ClaimTimeout at claim_submit")
        self.assertFalse(result.retryable)


class PipelineStageTests(unittest.TestCase):
    def test_every_stage_declares_a_phase(self):
        self.assertEqual(set(bot.STAGE_PHASES), set(Stage))
        self.assertEqual(bot.stage_phase(Stage.STARTUP), SubmitPhase.STARTUP)
        self.assertEqual(bot.stage_phase(Stage.UNKNOWN), SubmitPhase.POST_SUBMIT)

    def test_legacy_partitions_are_unchanged(self):
        self.assertEqual(
            bot.PRE_SUBMIT_STAGES, ("store", "player_id", "daily_gift_card", "claim_dialog")
        )
        self.assertEqual(bot.POST_SUBMIT_STAGES, ("claim_submit", "claim_verification"))

    def test_an_undeclared_stage_name_is_handled_as_post_submit(self):
        # Regression: run.log 2026-09-23 "error: TimeoutError at claim" -- a
        # stage in neither tuple kept the Supervisor retry token.
        for name in ("claim", "claim_inbox", "", None):
            with self.subTest(name=name):
                self.assertIs(bot.parse_stage(name), Stage.UNKNOWN)
                result = bot.failure_line(TimeoutError("t"), name)
                self.assertEqual(result.line, "error: ClaimTimeout at unknown")
                self.assertFalse(result.retryable)

    def test_stage_phase_refuses_a_bare_string(self):
        with self.assertRaises(TypeError):
            bot.stage_phase("store")

    def test_the_pipeline_only_steps_forward_one_stage(self):
        ref = [Stage.STARTUP]
        for stage in bot.PIPELINE_ORDER[1:]:
            bot.advance_stage(ref, stage)
            self.assertIs(ref[0], stage)
        for current, target in (
            (Stage.CLAIM_VERIFICATION, Stage.CLAIM_DIALOG),
            (Stage.CLAIM_SUBMIT, Stage.STORE),
            (Stage.STORE, Stage.CLAIM_DIALOG),
            (Stage.STORE, Stage.STORE),
            (Stage.UNKNOWN, Stage.STORE),
            (Stage.STORE, Stage.UNKNOWN),
        ):
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    bot.advance_stage([current], target)

    def test_a_post_submit_rejection_that_mentions_a_stage_is_not_retried(self):
        # Regression: "error: store rejected claim: Network error at store
        # transport" matched " at store" + " transport" and the whole claim
        # run was repeated after the click.
        rejection = RunResult.failed(
            "error: store rejected claim: Network error at store transport",
            Stage.CLAIM_VERIFICATION,
        )
        runs = []

        def fake_run():
            runs.append(1)
            return rejection

        with (
            patch.object(bot, "run", fake_run),
            patch.object(bot.time, "sleep", lambda _s: None),
        ):
            result = bot.run_with_retries()
        self.assertEqual(len(runs), 1)
        self.assertIs(result, rejection)

    def test_claim_flow_records_each_stage_as_a_declared_value(self):
        page = MagicMock()
        stages = []
        card = MagicMock()
        with (
            patch.object(bot, "find_daily_gift_card", lambda _p: card),
            patch.object(bot, "wait_for_claim_state", lambda _p, _c: False),
            patch.object(bot, "dialog_shows_claimed", lambda _d: False),
            patch.object(bot, "claim_confirmed", lambda _c, _d: True),
            patch.object(bot, "wait_for_validation", lambda _p: stages.append(ref[0])),
        ):
            ref = [Stage.STORE]
            result = bot.claim_flow(page, "123456", ref, latch_path=None)
        self.assertEqual(result, RunResult.claimed(Stage.CLAIM_VERIFICATION))
        self.assertEqual(stages, [Stage.PLAYER_ID])
        self.assertIs(ref[0], Stage.CLAIM_VERIFICATION)


class SubmitLatchTests(unittest.TestCase):
    NOW = datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)

    def latch(self, content: str | None) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "submit-latch.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        return path

    def test_the_live_latch_file_loads(self):
        # tests/fixtures/submit-latch.json is a copy of a real latch written
        # by the old code (2026-11-05 18:19 UTC claim).
        path = self.latch(None)
        shutil.copy(FIXTURES / "submit-latch.json", path)
        self.assertIs(bot.latch_state(path, self.NOW), LatchState.ARMED)
        self.assertTrue(bot.submit_blocked(path, self.NOW))
        tomorrow = self.NOW + timedelta(days=1)
        self.assertIs(bot.latch_state(path, tomorrow), LatchState.UNARMED)
        self.assertFalse(bot.submit_blocked(path, tomorrow))

    def test_no_file_is_unarmed(self):
        self.assertIs(bot.latch_state(None, self.NOW), LatchState.UNARMED)
        self.assertIs(bot.latch_state(self.latch(None), self.NOW), LatchState.UNARMED)

    def test_unreadable_or_legacy_latches_never_re_claim(self):
        for content in (
            "{",
            "",
            "[]",
            "{}",
            '{"submitted_at": 5}',
            '{"submitted_at": "not a date"}',
            '{"submitted_at": "2026-11-05T12:00:00"}',
            '{"armed": true}',
        ):
            with self.subTest(content=content):
                path = self.latch(content)
                self.assertIs(bot.latch_state(path, self.NOW), LatchState.ARMED_UNKNOWN)
                self.assertTrue(bot.submit_blocked(path, self.NOW))

    def test_a_future_dated_latch_never_re_claims(self):
        # Regression: a stamp from a later Toronto day read as unarmed.
        stamp = (self.NOW + timedelta(days=1)).isoformat()
        path = self.latch(json.dumps({"submitted_at": stamp}))
        self.assertIs(bot.latch_state(path, self.NOW), LatchState.ARMED_UNKNOWN)
        self.assertTrue(bot.submit_blocked(path, self.NOW))

    def test_a_naive_clock_never_re_claims(self):
        path = self.latch(None)
        shutil.copy(FIXTURES / "submit-latch.json", path)
        naive = datetime(2026, 11, 7, 12, 0)
        self.assertIs(bot.latch_state(path, naive), LatchState.ARMED_UNKNOWN)

    def test_the_latch_format_is_unchanged(self):
        path = self.latch(None)
        bot.record_submit(path, self.NOW)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw, {"submitted_at": self.NOW.isoformat()})
        self.assertIs(bot.latch_state(path, self.NOW), LatchState.ARMED)


class RunLockStateTests(unittest.TestCase):
    def lock(self, source: Path | str | None, *, age_seconds: float = 0) -> bot.RunLock:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "run.lock"
        if isinstance(source, Path):
            shutil.copy(source, path)
        elif source is not None:
            path.write_text(source, encoding="utf-8")
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))
        return bot.RunLock(path)

    def test_the_lock_states_partition(self):
        self.assertEqual(bot.LOCK_STEALABLE | bot.LOCK_HELD, frozenset(LockState))
        self.assertFalse(bot.LOCK_STEALABLE & bot.LOCK_HELD)

    def test_old_lock_files_load(self):
        legacy = FIXTURES / "run-lock-legacy.json"
        current = FIXTURES / "run-lock.json"
        alive = (bot.ProcessState.ALIVE, 134070000000000000)
        cases = [
            (legacy, alive, 0, LockState.UNVERIFIED_LIVE),
            (legacy, alive, bot.LOCK_UNVERIFIED_MAX_AGE_SECONDS + 60, LockState.UNVERIFIED_EXPIRED),
            (legacy, (bot.ProcessState.DEAD, None), 0, LockState.DEAD_HOLDER),
            (current, alive, 0, LockState.LIVE_HOLDER),
            (current, (bot.ProcessState.ALIVE, 1), 0, LockState.REUSED_PID),
            (current, (bot.ProcessState.UNKNOWN, None), 0, LockState.UNVERIFIED_LIVE),
            (current, (bot.ProcessState.ALIVE, None), 0, LockState.UNVERIFIED_LIVE),
        ]
        for source, identity, age, expected in cases:
            with self.subTest(source=source.name, identity=identity, age=age):
                lock = self.lock(source, age_seconds=age)
                with patch.object(bot, "_process_identity", return_value=identity):
                    self.assertIs(lock.state(), expected)
                    self.assertEqual(lock._stale(), expected in bot.LOCK_STEALABLE)

    def test_unreadable_and_malformed_locks(self):
        self.assertIs(self.lock("").state(), LockState.TORN_FRESH)
        self.assertIs(self.lock("{", age_seconds=60).state(), LockState.TORN_STALE)
        for content in ("[]", '{"pid": "1"}', '{"pid": 0}', '{"pid": true}', "{}"):
            with self.subTest(content=content):
                self.assertIs(self.lock(content).state(), LockState.MALFORMED)
        own = self.lock(json.dumps({"pid": os.getpid()}))
        self.assertIs(own.state(), LockState.OWN_PID)
        self.assertFalse(own._stale())

    def test_process_identity_is_a_declared_value(self):
        state, _ = bot._process_identity(os.getpid())
        self.assertIs(state, bot.ProcessState.ALIVE)
        self.assertIs(bot._process_identity(-1)[0], bot.ProcessState.DEAD)


if __name__ == "__main__":
    unittest.main()
