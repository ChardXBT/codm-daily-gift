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

## Notes

- Independent community project. Not affiliated with Activision, Call of Duty, or Coda Payments.
- The bot uses a fresh headless browser each run — no persistent profile or stored cookies.
- Users are responsible for their own Player ID and configuration.

## License

[MIT](LICENSE)
