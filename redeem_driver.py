"""Interactive redeem-code driver for callofduty.com/redemption.

Companion to SKILL.md (codm-auto-claim). callofduty.com silently hangs
non-real browsers, so this keeps one real Chrome window open and takes
simple commands through artifacts/redeem-session/cmd.txt while the agent
solves each CAPTCHA from the screenshots it drops in the same folder.

Run:  python redeem_driver.py          (headed; --headless to try headless)

Commands (write one line into artifacts/redeem-session/cmd.txt):
  fill <CODE>   fill UID + code, refresh CAPTCHA, screenshot captcha.png
  reshot        refresh + screenshot CAPTCHA again (after a wrong answer)
  answer <XXXX> type CAPTCHA text, click SUBMIT, screenshot dialog.png
  confirm       click CONFIRM, screenshot result.png
  ok            click OK to dismiss result/error popup
  nav           re-open the redemption page
  shot          full-page screenshot to page.png
  quit          close browser and exit

State after each command is written to artifacts/redeem-session/status.json
(seq increments; dump.confirm/dump.ok flag which popup is visible).
"""

import json
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SESS = ROOT / "artifacts" / "redeem-session"
SESS.mkdir(parents=True, exist_ok=True)
CMD = SESS / "cmd.txt"
STATUS = SESS / "status.json"


def read_uid():
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("CODM_UID="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no CODM_UID in .env")


UID = read_uid()
_seq = 0


def write_status(**kw):
    global _seq
    _seq += 1
    d = {"seq": _seq, "ts": time.time()}
    d.update(kw)
    STATUS.write_text(json.dumps(d, indent=1))


def dump(page):
    try:
        return page.evaluate(
            """() => {
              const vis = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const q = s => document.querySelector(s);
              const res = {};
              res.confirm = vis(q('#Confirm'));
              res.ok = vis(q('#btn_ok'));
              res.uid_val = q('#F_UID') ? q('#F_UID').value : null;
              res.code_val = q('#F_CDKEY') ? q('#F_CDKEY').value : null;
              res.vcode_val = q('#F_VCODE') ? q('#F_VCODE').value : null;
              res.submit_cls = q('#label_submit') ? q('#label_submit').className : null;
              let popup = null;
              for (const sel of ['#Confirm', '#btn_ok']) {
                const el = q(sel);
                if (vis(el)) {
                  let p = el;
                  for (let i = 0; i < 6 && p.parentElement; i++) {
                    p = p.parentElement;
                    if (vis(p) && p.innerText) {
                      const t = p.innerText.trim();
                      if (t.length > 0 && t.length < 700) popup = t;
                    }
                  }
                  if (popup) break;
                }
              }
              res.popup = popup;
              res.title = document.title;
              res.body = document.body.innerText.slice(0, 1200);
              return res;
            }"""
        )
    except Exception as e:
        return {"dump_error": repr(e)}


def wait_popup(page, want, timeout_ms=10000):
    d = {}
    for _ in range(int(timeout_ms / 500)):
        d = dump(page)
        if want == "confirm" and (d.get("confirm") or d.get("ok")):
            break
        if want == "ok" and d.get("ok"):
            break
        page.wait_for_timeout(500)
    return d


def shot_captcha(page):
    page.locator("#val_code").screenshot(path=str(SESS / "captcha.png"))


def open_redemption(page):
    try:
        page.goto(
            "https://www.callofduty.com/redemption",
            wait_until="commit",
            timeout=45000,
        )
    except Exception:
        pass
    try:
        page.wait_for_selector("#F_UID", timeout=45000, state="attached")
    except Exception:
        page.screenshot(path=str(SESS / "page.png"), full_page=True)
        write_status(state="load_failed", url=page.url, dump=dump(page))
    else:
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SESS / "page.png"), full_page=True)
        write_status(state="loaded", url=page.url, dump=dump(page))


def handle(page, cmd):
    parts = cmd.split()
    op = parts[0]

    if op == "fill":
        code = parts[1]
        page.fill("#F_UID", UID)
        page.fill("#F_CDKEY", code)
        page.fill("#F_VCODE", "")
        try:
            page.click('[alt="Refresh Icon"]', timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        shot_captcha(page)
        write_status(state="captcha_ready", code=code, dump=dump(page))

    elif op == "reshot":
        try:
            page.click('[alt="Refresh Icon"]', timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            page.wait_for_timeout(1000)
        shot_captcha(page)
        write_status(state="captcha_ready", dump=dump(page))

    elif op == "answer":
        text = parts[1]
        page.fill("#F_VCODE", text)
        page.evaluate(
            "document.querySelector('#label_submit')"
            " && document.querySelector('#label_submit').classList.remove('btn_confirm_gray')"
        )
        page.click("#label_submit")
        d = wait_popup(page, "confirm", 12000)
        page.screenshot(path=str(SESS / "dialog.png"))
        write_status(state="answered", dump=d)

    elif op == "confirm":
        page.click("#Confirm")
        d = wait_popup(page, "ok", 12000)
        page.screenshot(path=str(SESS / "result.png"))
        write_status(state="result", dump=d)

    elif op == "ok":
        try:
            page.click("#btn_ok", timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        write_status(state="dismissed", dump=dump(page))

    elif op == "nav":
        open_redemption(page)

    elif op == "shot":
        page.screenshot(path=str(SESS / "page.png"), full_page=True)
        write_status(state="shot", dump=dump(page))

    elif op == "quit":
        write_status(state="quit")
        raise SystemExit(0)

    else:
        write_status(state="bad_cmd", cmd=cmd)


def main():
    headless = "--headed" not in sys.argv and "--headless" in sys.argv
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                channel="chrome",
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = ctx.new_page()
        open_redemption(page)

        last_cmd = None
        while True:
            if CMD.exists():
                try:
                    cmd = CMD.read_text(encoding="utf-8").strip()
                except Exception:
                    cmd = None
                if cmd and cmd != last_cmd:
                    last_cmd = cmd
                    try:
                        handle(page, cmd)
                    except SystemExit:
                        raise
                    except Exception:
                        write_status(
                            state="error",
                            cmd=cmd,
                            error=traceback.format_exc()[-1200:],
                            dump=dump(page),
                        )
            time.sleep(0.25)


if __name__ == "__main__":
    main()
