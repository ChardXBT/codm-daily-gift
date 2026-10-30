"""Supervisor contract, log publication, and fail-closed claim guards.

These tests never open the store and never read the real player id.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC) - (?P<body>.*)$"
)


def _playwright(page=None, *, launch_error=None, close_error=None):
    browser = MagicMock()
    if close_error is not None:
        browser.close.side_effect = close_error
    browser.new_page.return_value = page if page is not None else MagicMock()
    playwright = MagicMock()
    if launch_error is not None:
        playwright.chromium.launch.side_effect = launch_error
    else:
        playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = False
    return manager, playwright, browser


class TorontoDayTests(unittest.TestCase):
    def test_eastern_daylight_and_standard_boundaries(self):
        cases = {
            "2026-03-08 06:59:00+00:00": "2026-03-08",
            "2026-03-08 07:00:00+00:00": "2026-03-08",
            "2026-10-30 03:30:00+00:00": "2026-10-29",
            "2026-10-30 04:00:00+00:00": "2026-10-30",
            "2026-11-01 05:59:00+00:00": "2026-11-01",
            "2026-11-01 06:00:00+00:00": "2026-11-01",
            "2026-01-15 04:59:00+00:00": "2026-01-14",
            "2026-01-15 05:00:00+00:00": "2026-01-15",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                moment = datetime.fromisoformat(raw)
                self.assertEqual(bot.toronto_day(moment).isoformat(), expected)

    def test_a_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            bot.toronto_day(datetime(2026, 10, 30, 12, 0, 0))


class FailureLineTests(unittest.TestCase):
    def test_post_submit_timeout_omits_the_supervisor_retry_token(self):
        line = bot.failure_line(TimeoutError("Timeout 30000ms exceeded"), "claim_submit")
        self.assertTrue(line.startswith("error:"))
        self.assertIn("claim_submit", line)
        self.assertNotIn("TimeoutError", line)
        self.assertFalse(bot.is_transient(line))
        for marker in bot.SUPERVISOR_TRANSIENT_MARKERS:
            self.assertNotIn(marker, line)

    def test_verification_timeout_is_also_a_hard_failure_line(self):
        line = bot.failure_line(TimeoutError("timed out"), "claim_verification")
        self.assertNotIn("TimeoutError", line)
        self.assertFalse(bot.is_transient(line))

    def test_pre_submit_timeout_keeps_the_supervisor_retry_token(self):
        line = bot.failure_line(TimeoutError("Timeout 60000ms exceeded"), "store")
        self.assertEqual(line, "error: TimeoutError at store")
        self.assertTrue(bot.is_transient(line))

    def test_disconnect_keeps_the_marker_and_drops_the_player_id(self):
        exc = Exception("net::ERR_INTERNET_DISCONNECTED while filling 9990001")
        line = bot.failure_line(exc, "store", uid="9990001")
        self.assertIn("net::ERR_INTERNET_DISCONNECTED", line)
        self.assertNotIn("9990001", line)
        self.assertNotIn("filling", line)
        self.assertTrue(bot.is_transient(line))

    def test_authority_and_blocked_tokens_are_not_copied_into_the_log(self):
        for message in (
            "net::ERR_CERT_AUTHORITY_INVALID",
            "net::ERR_BLOCKED_BY_CLIENT",
        ):
            with self.subTest(message=message):
                line = bot.failure_line(Exception(message), "player_id", uid="9990001")
                lowered = line.lower()
                self.assertNotIn("authority", lowered)
                self.assertNotIn("blocked", lowered)
                self.assertNotIn("auth", lowered)
                self.assertNotIn("9990001", line)
                self.assertTrue(line.endswith(" transport"))
                self.assertTrue(bot.is_transient(line))

    def test_store_detail_cannot_inject_a_log_line_or_a_secret(self):
        detail = bot.logged_store_error(
            "nope\n2026-01-01 00:00:00 UTC - claimed successfully TimeoutError 9990001",
            "9990001",
        )
        self.assertEqual(detail, bot.WITHHELD_STORE_DETAIL)
        self.assertNotIn("\n", detail)
        self.assertNotIn("9990001", detail)

    def test_an_ordinary_store_rejection_is_kept(self):
        text = "Sorry, you are not eligible to claim this item."
        self.assertEqual(bot.logged_store_error(text, "123"), text)


class RunFailureTests(unittest.TestCase):
    def test_run_reports_a_post_submit_timeout_without_a_retry_token(self):
        def fake_claim(page, uid, stage_ref, latch_path=None):
            stage_ref[0] = "claim_submit"
            raise TimeoutError("click timed out")

        manager, playwright, browser = _playwright()
        with (
            patch.object(bot, "sync_playwright", return_value=manager),
            patch.object(bot, "load_uid", return_value="123456"),
            patch.object(bot, "claim_flow", fake_claim),
        ):
            result = bot.run()
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("claim_submit", result)
        self.assertNotIn("TimeoutError", result)
        self.assertNotIn("click timed out", result)
        self.assertFalse(bot.is_transient(result))
        playwright.chromium.launch.assert_called_once_with(headless=True)
        browser.close.assert_called_once()

    def test_a_disconnect_before_submit_is_transient_and_redacted(self):
        page = MagicMock()
        page.goto.side_effect = Exception(
            "net::ERR_INTERNET_DISCONNECTED while filling 9990001"
        )
        manager, _, browser = _playwright(page)
        with (
            patch.object(bot, "sync_playwright", return_value=manager),
            patch.object(bot, "load_uid", return_value="9990001"),
        ):
            result = bot.run()
        self.assertIn("net::ERR_INTERNET_DISCONNECTED", result)
        self.assertNotIn("9990001", result)
        self.assertTrue(bot.is_transient(result))
        browser.close.assert_called_once()

    def test_browser_close_failure_does_not_discard_a_confirmed_claim(self):
        def fake_claim(page, uid, stage_ref, latch_path=None):
            return "claimed successfully"

        manager, _, _ = _playwright(close_error=RuntimeError("close failed"))
        with (
            patch.object(bot, "sync_playwright", return_value=manager),
            patch.object(bot, "load_uid", return_value="123456"),
            patch.object(bot, "claim_flow", fake_claim),
        ):
            result = bot.run()
        self.assertEqual(result, "claimed successfully")

    def test_a_launch_failure_is_a_logged_startup_error(self):
        manager, _, _ = _playwright(launch_error=RuntimeError("missing browser"))
        with (
            patch.object(bot, "sync_playwright", return_value=manager),
            patch.object(bot, "load_uid", return_value="123456"),
        ):
            result = bot.run()
        self.assertIn("startup", result)
        self.assertTrue(result.startswith("error:"), result)
        self.assertNotIn("missing browser", result)
        self.assertFalse(bot.is_transient(result))


class MainContractTests(unittest.TestCase):
    def _paths(self, directory: str):
        root = Path(directory)
        log = root / "run.log"
        log.write_text(
            "2026-10-01 00:00:00 UTC - claimed successfully\n",
            encoding="utf-8",
        )
        return log, root / "run.lock"

    def test_missing_player_id_is_logged_for_the_supervisor_and_exits_1(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            with (
                patch.object(bot, "load_dotenv", lambda *args, **kwargs: None),
                patch.dict(os.environ, {"CODM_UID": ""}, clear=False),
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log") as push,
                patch.object(
                    bot, "sync_playwright", side_effect=AssertionError("live browser")
                ),
            ):
                with self.assertRaises(SystemExit) as caught:
                    bot.main()
            self.assertEqual(caught.exception.code, 1)
            push.assert_called_once()
            body = LOG_LINE.match(log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertIsNotNone(body)
            self.assertEqual(body.group("body"), "Add UID UWU")

    def test_only_the_supervisor_success_phrases_exit_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            with (
                patch.object(bot, "load_uid", return_value="player-test"),
                patch.object(bot, "run_with_retries", return_value="GIFT CLAIMED"),
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log"),
                patch.object(
                    bot, "sync_playwright", side_effect=AssertionError("live browser")
                ),
            ):
                with self.assertRaises(SystemExit) as caught:
                    bot.main()
            self.assertEqual(caught.exception.code, 1)
            self.assertNotIn("PASS check inboxie UWU", log.read_text(encoding="utf-8"))

    def test_a_real_success_exits_zero_and_prints_the_pass_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            with (
                patch.object(bot, "load_uid", return_value="player-test"),
                patch.object(bot, "run_with_retries", return_value="claimed successfully"),
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log"),
                patch("builtins.print") as printed,
            ):
                bot.main()
            printed.assert_any_call("PASS check inboxie UWU")
            body = LOG_LINE.match(log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertIsNotNone(body)
            self.assertEqual(body.group("body"), "claimed successfully")

    def test_an_unexpected_exception_is_logged_without_its_message(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            with (
                patch.object(bot, "load_uid", return_value="player-test"),
                patch.object(
                    bot,
                    "run_with_retries",
                    side_effect=RuntimeError("boom secret"),
                ),
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log"),
            ):
                with self.assertRaises(SystemExit) as caught:
                    bot.main()
            self.assertEqual(caught.exception.code, 1)
            text = log.read_text(encoding="utf-8")
            self.assertIn("RuntimeError", text)
            self.assertIn("startup", text)
            self.assertNotIn("boom secret", text)
            self.assertNotIn("PASS check inboxie UWU", text)

    def test_a_live_lock_exits_75_without_an_error_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            lock.write_text(
                json.dumps({"pid": os.getpid()}),
                encoding="utf-8",
            )
            before = log.read_text(encoding="utf-8")
            with (
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log") as push,
                patch.object(
                    bot, "sync_playwright", side_effect=AssertionError("live browser")
                ),
            ):
                with self.assertRaises(SystemExit) as caught:
                    bot.main()
            self.assertEqual(caught.exception.code, 75)
            self.assertEqual(log.read_text(encoding="utf-8"), before)
            push.assert_not_called()

    def test_a_dead_lock_is_replaced_and_the_run_proceeds(self):
        with tempfile.TemporaryDirectory() as directory:
            log, lock = self._paths(directory)
            lock.write_text(json.dumps({"pid": 2_000_000_000}), encoding="utf-8")
            with (
                patch.object(bot, "load_uid", return_value="player-test"),
                patch.object(bot, "run_with_retries", return_value="already claimed"),
                patch.object(bot, "LOG_FILE", log),
                patch.object(bot, "LOCK_PATH", lock),
                patch.object(bot, "git_push_log"),
                patch.object(
                    bot, "sync_playwright", side_effect=AssertionError("live browser")
                ),
            ):
                bot.main()
            self.assertFalse(lock.exists())
            self.assertIn("already claimed", log.read_text(encoding="utf-8").splitlines()[-1])

    def test_player_id_comes_from_the_repo_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("CODM_UID=from-file\n", encoding="utf-8")
            with (
                patch.object(bot, "ENV_PATH", env_file),
                patch.dict(os.environ, {"CODM_UID": "from-env"}, clear=False),
            ):
                got = bot.load_uid()
            if got != "from-file":
                raise AssertionError("player id was not taken from the repo env file")


class LogTests(unittest.TestCase):
    def test_a_log_line_matches_the_supervisor_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            with patch.object(bot, "LOG_FILE", log):
                bot.log_result("claimed successfully")
            line = log.read_text(encoding="utf-8").splitlines()[-1]
            match = LOG_LINE.match(line)
            self.assertIsNotNone(match)
            self.assertEqual(match.group("body"), "claimed successfully")

    def test_a_missing_trailing_newline_does_not_glue_the_next_result(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.write_bytes(b"2026-10-01 00:00:00 UTC - claimed successfully")
            with patch.object(bot, "LOG_FILE", log):
                bot.log_result("already claimed")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "2026-10-01 00:00:00 UTC - claimed successfully")
            self.assertIsNotNone(LOG_LINE.match(lines[1]))
            self.assertTrue(lines[1].endswith("already claimed"))

    def test_a_newline_in_the_result_cannot_become_a_second_log_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            with patch.object(bot, "LOG_FILE", log):
                bot.log_result(
                    "already claimed\n2026-01-01 00:00:00 UTC - error: injected"
                )
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIsNotNone(LOG_LINE.match(lines[0]))


class GitPublicationTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "codm-test@example.com"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "codm-test"],
            cwd=repo, check=True, capture_output=True,
        )
        log = repo / "run.log"
        log.write_text("2026-10-01 00:00:00 UTC - claimed successfully\n", encoding="utf-8")
        subprocess.run(["git", "add", "run.log"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo, check=True, capture_output=True,
        )
        return repo

    def test_log_commit_excludes_other_staged_files_and_ignores_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._repo(root)
            notes = repo / "notes.txt"
            notes.write_text("do not publish\n", encoding="utf-8")
            subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True, capture_output=True)
            log = repo / "run.log"
            log.write_text(
                log.read_text(encoding="utf-8") + "2026-10-30 00:00:00 UTC - already claimed\n",
                encoding="utf-8",
            )
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            previous = Path.cwd()
            os.chdir(elsewhere)
            try:
                with patch.object(bot, "LOG_FILE", log):
                    if log.parent != repo:
                        raise AssertionError("test log escaped the temp repo")
                    bot.git_push_log()
            finally:
                os.chdir(previous)
            show = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            self.assertEqual(show.stdout.strip(), "run.log")
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            self.assertIn("notes.txt", staged.stdout.splitlines())
            message = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            self.assertTrue(message.stdout.startswith("log: "))

    def test_an_unpushed_log_commit_is_pushed_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._repo(root)
            bare = root / "bare.git"
            subprocess.run(
                ["git", "init", "--bare", "-b", "master", str(bare)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(bare)],
                cwd=repo, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=repo, check=True, capture_output=True,
            )
            log = repo / "run.log"
            log.write_text(
                log.read_text(encoding="utf-8") + "2026-10-30 01:00:00 UTC - already claimed\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "run.log"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "log: 2026-10-30 01:00"],
                cwd=repo, check=True, capture_output=True,
            )
            local = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            with patch.object(bot, "LOG_FILE", log):
                bot.git_push_log()
            remote = subprocess.run(
                ["git", "rev-parse", "master"],
                cwd=bare, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(remote, local)

    def test_an_unpushed_code_commit_is_not_pushed_with_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._repo(root)
            bare = root / "bare.git"
            subprocess.run(
                ["git", "init", "--bare", "-b", "master", str(bare)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(bare)],
                cwd=repo, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=repo, check=True, capture_output=True,
            )
            published = subprocess.run(
                ["git", "rev-parse", "master"],
                cwd=bare, check=True, capture_output=True, text=True,
            ).stdout.strip()
            notes = repo / "notes.txt"
            notes.write_text("local only\n", encoding="utf-8")
            subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "local code"],
                cwd=repo, check=True, capture_output=True,
            )
            log = repo / "run.log"
            with patch.object(bot, "LOG_FILE", log):
                bot.git_push_log()
            remote = subprocess.run(
                ["git", "rev-parse", "master"],
                cwd=bare, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(remote, published)

    def test_a_new_log_line_does_not_publish_an_older_code_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._repo(root)
            bare = root / "bare.git"
            subprocess.run(
                ["git", "init", "--bare", "-b", "master", str(bare)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(bare)],
                cwd=repo, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=repo, check=True, capture_output=True,
            )
            published = subprocess.run(
                ["git", "rev-parse", "master"],
                cwd=bare, check=True, capture_output=True, text=True,
            ).stdout.strip()
            notes = repo / "notes.txt"
            notes.write_text("local only\n", encoding="utf-8")
            subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "local code"],
                cwd=repo, check=True, capture_output=True,
            )
            log = repo / "run.log"
            log.write_text(
                log.read_text(encoding="utf-8") + "2026-10-30 02:00:00 UTC - already claimed\n",
                encoding="utf-8",
            )
            with patch.object(bot, "LOG_FILE", log):
                bot.git_push_log()
            remote = subprocess.run(
                ["git", "rev-parse", "master"],
                cwd=bare, check=True, capture_output=True, text=True,
            ).stdout.strip()
            local = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            committed = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=repo, check=True, capture_output=True, text=True,
            )
            self.assertEqual(committed.stdout.strip(), "run.log")
            self.assertNotEqual(local, published)
            self.assertEqual(remote, published)


class LatchTests(unittest.TestCase):
    def test_a_corrupt_latch_blocks_the_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            latch = Path(directory) / "submit-latch.json"
            latch.write_text("{", encoding="utf-8")
            self.assertTrue(bot.submit_blocked(latch))

    def test_a_latch_without_a_timezone_blocks_the_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            latch = Path(directory) / "submit-latch.json"
            latch.write_text(
                json.dumps({"submitted_at": "2026-10-30T12:00:00"}),
                encoding="utf-8",
            )
            self.assertTrue(bot.submit_blocked(latch))

    def test_record_submit_is_the_same_toronto_day(self):
        with tempfile.TemporaryDirectory() as directory:
            latch = Path(directory) / "submit-latch.json"
            moment = datetime(2026, 10, 30, 3, 30, tzinfo=timezone.utc)
            bot.record_submit(latch, moment)
            self.assertTrue(bot.submit_blocked(latch, moment))
            later = moment + timedelta(days=2)
            self.assertFalse(bot.submit_blocked(latch, later))
            raw = json.loads(latch.read_text(encoding="utf-8"))
            self.assertEqual(set(raw), {"submitted_at"})


if __name__ == "__main__":
    unittest.main()
