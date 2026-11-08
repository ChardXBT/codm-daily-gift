"""A stalled step should cost 45 seconds, not a whole day.

The job runs once a day. Before this, a single TimeoutError anywhere in
the walk ended the run and the next attempt was 24 hours away -- even
though run.log shows a TimeoutError at 02:46:40 and a clean
"already claimed" 75 seconds later, from the same store, the same night.

Run with:  .venv/Scripts/python.exe -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
from bot import RunResult, Stage  # noqa: E402


def stall(stage: str) -> RunResult:
    return bot.failure_line(TimeoutError("Timeout 30000ms exceeded"), stage)


class RetryTests(unittest.TestCase):
    def run_with(self, outcomes):
        remaining = list(outcomes)
        calls = {"n": 0}

        def fake_run():
            calls["n"] += 1
            return remaining.pop(0)

        with (
            patch.object(bot, "run", fake_run),
            patch.object(bot.time, "sleep", lambda _s: None),
        ):
            result = bot.run_with_retries()
        return calls["n"], result

    def test_a_stalled_step_gets_another_look(self):
        calls, result = self.run_with([stall("store"), RunResult.claimed()])
        self.assertEqual(calls, 2)
        self.assertEqual(result.line, "claimed successfully")

    def test_a_clean_claim_costs_one_attempt(self):
        calls, result = self.run_with([RunResult.claimed()])
        self.assertEqual(calls, 1)
        self.assertEqual(result, RunResult.claimed())

    def test_already_claimed_is_not_an_error_and_is_not_retried(self):
        calls, result = self.run_with([RunResult.already_claimed()])
        self.assertEqual(calls, 1)
        self.assertEqual(result.line, "already claimed")

    def test_a_persistent_stall_gives_up_and_reports_it(self):
        stalls = [stall("store")] * bot.RUN_ATTEMPTS
        calls, result = self.run_with(stalls)
        self.assertEqual(calls, bot.RUN_ATTEMPTS)
        self.assertEqual(result.line, "error: TimeoutError at store")

    def test_a_missing_control_is_not_retried(self):
        # Only a stall is transient. If the Claim Gift text is not on the
        # card at all, trying twice more just delays the report.
        missing = RunResult.failed(
            "error: Claim Gift button not found", Stage.CLAIM_DIALOG
        )
        calls, result = self.run_with([missing])
        self.assertEqual(calls, 1)
        self.assertEqual(result.line, "error: Claim Gift button not found")

    def test_an_unverifiable_claim_is_not_retried(self):
        # Re-running after the claim may have gone through risks acting
        # twice on a once-daily gift. Report the ambiguity instead.
        unverified = RunResult.failed(
            "error: Unable to verify claim result", Stage.CLAIM_VERIFICATION
        )
        calls, _ = self.run_with([unverified])
        self.assertEqual(calls, 1)

    def test_a_submit_timeout_is_never_retried(self):
        for stage in ("claim_submit", "claim_verification"):
            with self.subTest(stage=stage):
                calls, result = self.run_with([stall(stage)])
                self.assertEqual(calls, 1)
                self.assertEqual(result.line, f"error: ClaimTimeout at {stage}")
                # The old line for this case cannot even be built now.
                with self.assertRaises(ValueError):
                    RunResult.failed(f"error: TimeoutError at {stage}", Stage(stage))

    def test_confirmation_text_counts_even_if_button_remains_visible(self):
        dialog = Mock()
        dialog.inner_text.return_value = "GIFT CLAIMED"
        with patch.object(bot, "is_claimed", return_value=False):
            self.assertTrue(bot.claim_confirmed(Mock(), dialog))

    def test_pre_submit_dialog_stall_remains_retryable(self):
        calls, result = self.run_with(
            [stall("claim_dialog"), RunResult.already_claimed()]
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.line, "already claimed")

    def test_the_stage_names_where_it_stalled(self):
        # "error: TimeoutError" alone never said which step was slow.
        result = stall("player_id")
        self.assertEqual(result.line, "error: TimeoutError at player_id")
        self.assertTrue(bot.is_transient(result))
        self.assertFalse(bot.is_transient(RunResult.failed(
            "error: Claim Gift button not found", Stage.CLAIM_DIALOG
        )))

    def test_a_bare_log_string_is_never_parsed_for_retryability(self):
        with self.assertRaises(TypeError):
            bot.is_transient("error: TimeoutError at player_id")
        with self.assertRaises(TypeError):
            bot.is_success("claimed successfully")

    def test_retry_waits_the_full_delay(self):
        sleeps = []
        remaining = [stall("store"), RunResult.already_claimed()]

        def fake_run():
            return remaining.pop(0)

        with (
            patch.object(bot, "run", fake_run),
            patch.object(bot.time, "sleep", sleeps.append),
        ):
            result = bot.run_with_retries()
        self.assertEqual(result.line, "already claimed")
        self.assertEqual(sleeps, [bot.RETRY_DELAY_SECONDS])

    def test_pacing_constants_stay_at_their_guards(self):
        self.assertEqual(bot.RUN_ATTEMPTS, 3)
        self.assertEqual(bot.RETRY_DELAY_SECONDS, 45)
        self.assertEqual(bot.CLAIM_STATE_SETTLE_MS, 8000)
        self.assertEqual(bot.CARD_VISIBLE_TIMEOUT_MS, 15000)

    def test_a_pre_submit_transport_failure_is_retried(self):
        transport = bot.failure_line(
            Exception("net::ERR_CONNECTION_RESET"), "store"
        )
        self.assertEqual(transport.line, "error: Exception at store transport")
        calls, result = self.run_with([transport, RunResult.already_claimed()])
        self.assertEqual(calls, 2)
        self.assertEqual(result.line, "already claimed")

    def test_a_post_submit_claim_timeout_line_is_not_retried(self):
        calls, result = self.run_with([stall("claim_submit")])
        self.assertEqual(calls, 1)
        self.assertEqual(result.line, "error: ClaimTimeout at claim_submit")
        self.assertNotIn("TimeoutError", result.line)


if __name__ == "__main__":
    unittest.main()
