# CODM Daily Gift Bot

Automatically claims the free Daily Gift from the Canadian Call of Duty: Mobile Web Store.

## Features

- One-shot execution — runs once, exits cleanly
- Headless Playwright browser (no manual Chrome needed)
- Semantically finds the Daily Gift (not by position)
- Detects already-claimed state
- Verifies the result
- UID stored locally in `.env`
- Logs each run to `run.log` and pushes to GitHub

## Requirements

- Python 3.10+
- Playwright + Chromium
- Git (for log syncing)

## Installation

```bash
git clone <repo-url>
cd codm-daily-gift
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and add your CODM Player ID:

```bash
cp .env.example .env
```

```env
CODM_UID=your_call_of_duty_player_id
```

Find your Player ID in-game: **Player Profile > BASIC**.

> `.env` is gitignored and will never be committed. Do not share it.

## Usage

```bash
python bot.py
```

One execution performs one Daily Gift attempt, logs the result, and exits. There is no built-in scheduler — use cron, Task Scheduler, or similar to run daily.

## Output

Successful claim or already-claimed:

```
Starting CODM Daily Gift bot...
Opening store...
Entering Player ID...
Finding Daily Gift...
Daily Gift already claimed.
PASS check inboxie UWU
2026-09-17 15:30:00 UTC - already claimed
```

Missing UID:

```
Starting CODM Daily Gift bot...
Add UID UWU
```

## Log

Each run appends a line to `run.log` with the UTC timestamp and result. The log is auto-committed and pushed to GitHub after every run.

Before forking or using your own repo, **clear the log**:

```bash
> run.log
```

Then commit and push the empty file:

```bash
git add run.log
git commit -m "clear log"
git push
```

## Supervisor contract

An external supervisor decides each day's outcome by grepping `run.log`
(case-insensitive regexes, one result line per run) and reading the exit
code. These strings and codes are a wire contract: keep them byte-identical.
`tests/test_state_machines.py` pins them (set `CODM_SUPERVISOR_ROOT` to a
supervisor checkout to cross-check its `config.yaml` as well).

| Supervisor reads | Value | Bot meaning |
| --- | --- | --- |
| success pattern | `claimed successfully`, `already claimed` | `ResultKind.CLAIMED` / `ALREADY_CLAIMED`, exit 0 |
| failure pattern | `error:`, `Add UID UWU`, `Traceback (most recent call last):` | `ResultKind.FAILED` / `CONFIG_MISSING`, exit 1 |
| transient pattern (relaunch) | `TimeoutError`, `net::ERR_INTERNET_DISCONNECTED`, `Could not resolve host`, `getaddrinfo failed` | only ever logged for a failure **before** the claim click |
| attention words (latch) | `signin`, `sign-in`, `captcha`, `challenge`, `auth`, `session_expired`, `account_restricted`, `blocked` | store and exception text carrying these is withheld |
| exit code | `0` / `1` / `75` | success / any other line / another run holds the lock (nothing logged) |

The supervisor checks transient first, then attention, then failure, then
success. A transient token after the claim click would relaunch the bot and
click Claim a second time, so:

- each run returns a `RunResult` (`kind`, `line`, `stage`, `stalled`).
  Retryability and the exit code come from those fields, never from parsing
  the line, and a post-submit result that carries a transient token cannot be
  constructed;
- every pipeline `Stage` declares its `SubmitPhase` (startup, pre-submit,
  post-submit). A stage name outside the vocabulary is `Stage.UNKNOWN` and is
  handled as post-submit;
- the submit latch (`artifacts/submit-latch.json`) reads as `LatchState`.
  Only a stamp from an earlier Toronto day is unarmed; a missing file is
  unarmed; anything unreadable, legacy or future-dated is `ARMED_UNKNOWN` and
  blocks the click for the day. A future-dated latch (a clock that ran ahead)
  keeps blocking until that day arrives; after checking the in-game inbox,
  delete `artifacts/submit-latch.json` by hand to clear it.

Two log lines differ from older versions, both after the claim click only: an
exception class whose name contains `TimeoutError` (for example
`ConnectTimeoutError`) is logged as `ClaimTimeout`, and an undeclared stage
is logged as `at unknown`. Both used to carry the Supervisor's retry token.

## Notes

- Independent community project. Not affiliated with Activision, Call of Duty, or Coda Payments.
- The bot uses a fresh headless browser each run — no persistent profile or stored cookies.
- Users are responsible for their own Player ID and configuration.

## License

[MIT](LICENSE)
