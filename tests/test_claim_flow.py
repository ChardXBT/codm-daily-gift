"""Drive claim_flow against a local copy of the store's claim markup.

The fixture mirrors what the live store renders (probed 2026-09-22):

- the Claim dialog is ".sheet-dialog.freebie-redeem-modal" inside a
  [role='dialog'] wrapper that has no box, so Playwright never reports
  that wrapper as visible -- the old locator timed out on every claim;
- a claimed card first renders "CLAIM GIFT" and only swaps in the
  purchased overlay once the store hydrates the player's claim state;
- after a claim the dialog retitles itself "Gift Claimed".

Run with:  .venv/Scripts/python.exe -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402

STORE = """
<style>.sheet-dialog__title { text-transform: uppercase; }</style>
<input id="userId">
<div id="rank"></div>
<div data-testid="sku-item-inner-container-daily" id="card">
  <div class="sku-info-section"><span>DAILY GIFT</span></div>
  <div class="price-section"><span id="price">CLAIM GIFT</span></div>
</div>
<div role="dialog" id="wrapper" style="display:none; width:0; height:0; overflow:visible">
  <div class="sheet-dialog freebie-redeem-modal"
       style="position:fixed; top:40px; left:40px; width:600px; height:300px">
    <div data-testid="freebie-redeem-modal">
      <span class="sheet-dialog__title" id="title">Claim Gift</span>
    </div>
    <p data-testid="instruction-text" id="instruction">You are about to claim your <b>Gift</b></p>
    <div id="footer">
      <button type="button" data-testid="claim-button" id="claim">CLAIM</button>
    </div>
  </div>
</div>
<script>
  window.claims = 0;
  const mode = new URLSearchParams(location.hash.slice(1)).get("mode");
  const markClaimed = () => {
    const overlay = document.createElement("div");
    overlay.dataset.testid = "purchased-overlay";
    overlay.textContent = "Claimed";
    document.getElementById("card").prepend(overlay);
    document.getElementById("price").textContent = "Claimed";
  };
  const showRedeemed = () => {
    document.getElementById("title").textContent = "Gift Claimed";
    document.getElementById("instruction").textContent =
      "Your Gift has been sent to your COD:M Account. Please check your inbox!";
    document.getElementById("claim").remove();
  };
  document.getElementById("userId").addEventListener("blur", () => {
    document.getElementById("rank").textContent = "MP Rank: 42";
    if (mode === "late-claimed") setTimeout(markClaimed, 1500);
  });
  document.getElementById("card").addEventListener("click", () => {
    document.getElementById("wrapper").style.display = "block";
    if (mode === "claimed-dialog-only") showRedeemed();
  });
  document.getElementById("claim").addEventListener("click", (e) => {
    window.claims += 1;
    e.target.disabled = true;
    setTimeout(() => {
      if (mode === "rejected") {
        const err = document.createElement("span");
        err.dataset.testid = "error-prompt";
        err.textContent = "Sorry, you are not eligible to claim this item.";
        document.getElementById("footer").prepend(err);
        return;
      }
      showRedeemed();
      markClaimed();
    }, 800);
  });
</script>
"""


class ClaimFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def flow(self, mode: str):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route("https://store.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=STORE))
        page.goto(f"https://store.test/codm#mode={mode}")
        stage = ["store"]
        with patch.object(bot, "CLAIM_STATE_SETTLE_MS", 3000):
            result = bot.claim_flow(page, "123", stage)
        return result, page.evaluate("window.claims"), stage[0]

    def test_unclaimed_gift_is_claimed_once_and_verified(self):
        result, claims, _ = self.flow("unclaimed")
        self.assertEqual(result, "claimed successfully")
        self.assertEqual(claims, 1)

    def test_claim_state_that_renders_late_is_not_claimed_again(self):
        # 2026-09-23 01:56: the card still said CLAIM GIFT when checked,
        # the bot clicked it, and the run died at claim_dialog.
        result, claims, _ = self.flow("late-claimed")
        self.assertEqual(result, "already claimed")
        self.assertEqual(claims, 0)

    def test_dialog_that_already_says_claimed_is_not_submitted(self):
        result, claims, _ = self.flow("claimed-dialog-only")
        self.assertEqual(result, "already claimed")
        self.assertEqual(claims, 0)

    def test_store_rejection_is_reported_not_retried(self):
        result, claims, stage = self.flow("rejected")
        self.assertTrue(result.startswith("error: store rejected claim"), result)
        self.assertIn("not eligible", result)
        self.assertEqual(claims, 1)
        self.assertFalse(bot.is_transient(result))
        self.assertEqual(stage, "claim_verification")


if __name__ == "__main__":
    unittest.main()
