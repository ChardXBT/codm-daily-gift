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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


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
        calls, result = self.run_with(["error: TimeoutError at store", "claimed"])
        self.assertEqual(calls, 2)
        self.assertEqual(result, "claimed")

    def test_a_clean_claim_costs_one_attempt(self):
        calls, result = self.run_with(["GIFT CLAIMED"])
        self.assertEqual(calls, 1)
        self.assertEqual(result, "GIFT CLAIMED")

    def test_already_claimed_is_not_an_error_and_is_not_retried(self):
        calls, result = self.run_with(["already claimed"])
        self.assertEqual(calls, 1)
        self.assertEqual(result, "already claimed")

    def test_a_persistent_stall_gives_up_and_reports_it(self):
        stalls = ["error: TimeoutError at claim"] * bot.RUN_ATTEMPTS
        calls, result = self.run_with(stalls)
        self.assertEqual(calls, bot.RUN_ATTEMPTS)
        self.assertEqual(result, "error: TimeoutError at claim")

    def test_a_missing_control_is_not_retried(self):
        # Only a stall is transient. If the Claim Gift text is not on the
        # card at all, trying twice more just delays the report.
        calls, result = self.run_with(["error: Claim Gift button not found"])
        self.assertEqual(calls, 1)
        self.assertEqual(result, "error: Claim Gift button not found")

    def test_an_unverifiable_claim_is_not_retried(self):
        # Re-running after the claim may have gone through risks acting
        # twice on a once-daily gift. Report the ambiguity instead.
        calls, _ = self.run_with(["error: Unable to verify claim result"])
        self.assertEqual(calls, 1)

    def test_the_stage_names_where_it_stalled(self):
        # "error: TimeoutError" alone never said which step was slow.
        self.assertTrue(bot.is_transient("error: TimeoutError at player_id"))
        self.assertFalse(bot.is_transient("error: Claim Gift button not found"))


if __name__ == "__main__":
    unittest.main()
