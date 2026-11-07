# Implicit State Machines — codm-daily-gift

Audit of tracked source (`bot.py`, `tests/`). No production code was changed.
This bot encodes every lifecycle as free-form strings, file presence, and
timestamp inference instead of declared state types; an external Supervisor
parses `run.log` by substring, which makes the implicit machines load-bearing.

## run-result-protocol

The outcome of a claim run — success kind, failure stage, retryability — is a
product state encoded in one free-text line, parsed by substring in at least
four places inside the repo plus the external Supervisor.

**States**
- `claimed successfully`, `already claimed` (only `SUCCESS_RESULTS` members, bot.py:33)
- `Add UID UWU` (config-missing; neither success nor `error:`)
- `error: <TypeName> at <stage> [<markers>] [transport]` (transient vs final
  distinguished purely by which stage string and which marker substrings appear)
- `error: store rejected claim: <sanitized detail>` (bot.py:695)
- `error: claim already submitted today; result still unverified` (bot.py:58–60)
- `error: Claim result unverified after submit` (bot.py:697), `error: Claim Gift
  button not found` (bot.py:656), `error: could not record claim submit`
  (bot.py:684), `error: expected one Daily Gift card…` (bot.py:640),
  `error: empty result` (bot.py:111)
- Process exit tri-state `0 | 1 | 75` ("busy, attempt refunded") at
  bot.py:772, 789, 795 — part of the same external contract.

**Encoded as** — a single line string. Success = exact membership in a frozenset
(bot.py:507–508). Retryability = `startswith("error:")` AND (`f" at {stage}"`
substring in a pre-submit list OR ` transport` suffix OR a transient-marker
substring) (bot.py:556–570). `"error" not in result` gates the retry loop
(bot.py:703). The Supervisor independently greps for `SUCCESS_RESULTS`,
`SUPERVISOR_TRANSIENT_MARKERS`, `SUPERVISOR_ATTENTION_FRAGMENTS` (bot.py:31–55);
comments say those strings "must stay exact" copies of the job's config.

**Invalid combos** — any post-submit result whose text contains a pre-submit
stage name plus a transient marker (`net::ERR_`, `TimeoutError`, …) flips to
"transient" → retry after a claim may already be submitted. Any line containing
`auth`, `blocked`, `captcha`, etc. latches the job as an account problem. The
code hand-guards both (TimeoutError→`ClaimTimeout` rename at bot.py:465, marker
scrubbing in `failure_line`, withholding in `logged_store_error`,
`_attention_or_contract_fragment`) — the guards exist precisely because the
encoding permits these combos. A new `return "error: …"` site that embeds a
stage name or unfiltered exception text silently bypasses all of it.

**Transitions** — set at every `claim_flow` return (bot.py:640, 644, 650, 656,
668, 684, 690, 694–695, 697), `failure_line` (bot.py:451–479), `run` (bot.py:715,
744), `run_with_retries` (bot.py:703–709), `main` (bot.py:785–790); read by
`is_transient`, `is_success`, the retry loop, and the Supervisor's log grep.

**Risk** — high. A formatting drift (new stage name, unscrubbed Playwright/store
text) either causes a same-day second claim submission on a once-daily gift, or
latches the job as "attention needed" and the gift is never claimed again.

## pipeline-stage-strings

The claim pipeline's current phase is a magic string in a mutable one-element
list, with the safe/dangerous partition duplicated in two tuples that don't
cover all values.

**States** — `startup` (bot.py:716, 784), `store` (bot.py:723), `player_id`
(bot.py:625), `daily_gift_card` (bot.py:635), `claim_dialog` (bot.py:652),
`claim_submit` (bot.py:679), `claim_verification` (bot.py:687). Seven mutually
exclusive phases; `PRE_SUBMIT_STAGES`/`POST_SUBMIT_STAGES` (bot.py:56–57) cover
six — `startup` is in neither tuple.

**Encoded as** — `stage_ref[0]` string assignments threaded through
`claim_flow`/`run`; membership tests `stage in POST_SUBMIT_STAGES` /
`in PRE_SUBMIT_STAGES` decide whether the Supervisor's retry tokens survive into
the log line (bot.py:462–475) and whether the result is retryable
(bot.py:560–562).

**Invalid combos** — a stage value in neither tuple (today only `startup`, but
any renamed/added phase — e.g. splitting verification into `claim_inbox` — lands
here by default): `failure_line` takes the pre-submit branch, so `TimeoutError`
and transient markers stay in the line, while `is_transient` says not-retryable.
In-repo behavior stays consistent, but the Supervisor greps `run.log` for the
raw markers → relaunches → second claim click. run.log shows this hazard was
already hit once (`error: TimeoutError at claim` — a stage name that existed in
no list — precedes the `claim_dialog` rename).

**Transitions** — written at bot.py:625, 635, 652, 679, 687, 716, 723; read at
bot.py:462–475, 560–562, 744.

**Risk** — high. Same blast radius as run-result-protocol: a missed tuple update
converts a post-submit stall into a Supervisor retry and a duplicate submit.

## submit-latch-file

"Has a claim click already been armed today?" is inferred from the existence,
parseability, and timestamp of `artifacts/submit-latch.json`.

**States** — (a) unarmed: file missing, or `submitted_at` on a different Toronto
day; (b) armed: well-formed `{"submitted_at": <ISO tz-aware>}` for the current
Toronto day; (c) armed-unknown: corrupt JSON, non-dict, missing key, naive or
unparseable timestamp — all collapse to "armed" (fail closed).

**Encoded as** — file existence + JSON schema + `toronto_day(submitted_at) ==
toronto_day(now)` (bot.py:387–408). No explicit status field; two different
encodings mean "unarmed", four different corruptions mean "armed".

**Invalid combos** — state (c) merges "click was sent, record unreadable" with
"file truncated by a crash before any click": both block the day, so a crashed
run that never reached the store can still cost the daily claim. A tz-aware
stamp written by a clock in the future lands on a different Toronto day →
silently unarmed → re-click allowed.

**Transitions** — armed by `record_submit` (bot.py:411–422, atomic tmp+rename)
called at bot.py:682 immediately before the confirm click; read only by
`submit_blocked` at bot.py:649.

**Risk** — medium. The latch is the last guard against a second submit; its
fail-closed design trades a possible missed day (corrupt file on a run that
never submitted) for never double-claiming.

## run-lock-file

Mutual exclusion between daily runs is a state machine encoded in lock-file
presence, JSON contents, OS-level pid liveness, and two unrelated age
thresholds.

**States** — absent; live-owned (pid alive AND `created` identity matches);
stealable (pid dead / non-int / non-dict JSON); unverifiable-live (pid alive but
identity unprovable → live until `mtime` age > 3600 s, bot.py:359); fresh-torn
(unparseable file ≤ 5 s old → treated live, bot.py:339); plus `_process_identity`'s
string tri-state `"alive" | "dead" | "unknown"` (bot.py:215, 347).

**Encoded as** — file existence + `{"pid": int, "created": int}` JSON + pid
liveness probe + mtime vs `5` and `LOCK_UNVERIFIED_MAX_AGE_SECONDS` thresholds
(bot.py:331–359). Six states, no declared model; the `5`-second constant is a
bare literal.

**Invalid combos** — `{pid alive, "created" absent}` (legacy lock): held until an
hour old even if the holder is unrelated — a reused pid or a foreign process
blocks every launch with exit 75 for up to an hour, and since 75 refunds the
attempt, the Supervisor keeps not counting it — a stuck job. `{pid ==
os.getpid()}` in a leftover file is treated live forever (bot.py:345–346) — a
recycled own-pid can never be stolen.

**Transitions** — created at `RunLock.acquire` (bot.py:297–323); evaluated by
`_stale` (bot.py:331–359); removed conditionally by `release` (bot.py:371–384,
only if `pid` still ours); read by `holder_summary` (bot.py:361–369) and `main`
(bot.py:767).

**Risk** — medium. The repo's own test comments document the realized bug: a
live-pid-mismatch lock made every launch exit 75 "and the gift was never
claimed again" (tests/test_contract.py:340–346). Wrong "alive" verdicts stall
the job; wrong "dead" verdicts allow two concurrent claim runs.

## card-claim-dom-state

Whether the daily gift is already claimed is inferred from element presence and
text inclusion/exclusion on a partially-hydrated store card and dialog.

**States** — unclaimed (no purchased overlay, `CLAIM GIFT` text present);
claimed (overlay visible, OR text has `Claimed` and not `CLAIM GIFT`);
ambiguous (both `Claimed` and `CLAIM GIFT` present — representable during
hydration); dialog-claimed (title contains `GIFT CLAIMED`, or `Claimed` +
`inbox`, bot.py:581–583); dialog-error (`error-prompt` visible, bot.py:596–603).

**Encoded as** — `overlay.count() > 0 && is_visible()` OR
`CLAIMED_TEXT in text and CLAIM_GIFT_TEXT not in text` (bot.py:548–553); dialog
by lowercased substring pairs. Claimed-ness is `True/False` at three different
read sites with three different inference rules (`is_claimed`,
`dialog_shows_claimed`, `claim_confirmed`).

**Invalid combos** — the ambiguous state (`Claimed` + `CLAIM GIFT` together, or
overlay present but not yet visible) evaluates to "unclaimed" at bot.py:553,
which is the click-again direction; only the 8 s settle loop (bot.py:606–614),
the dialog re-check (bot.py:667), and the submit latch stand between it and a
duplicate submit. The three readers can also disagree (card says unclaimed while
dialog says claimed), and the flow papers over it with "either counts"
(bot.py:586–593).

**Transitions** — state lives in the remote store DOM; read at bot.py:610,
643, 667, 689; the `already claimed` / click decision fans out from
`claim_flow` (bot.py:643–697).

**Risk** — low-medium. Mitigations make it hard to hit, but every guard is a
timing loop rather than a state read: a store that hydrates slower than 8 s, or
renders both markers, degrades to the latch as the only barrier.
