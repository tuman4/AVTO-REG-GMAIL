"""Playwright driver shim.

patchright is a drop-in patched build of Playwright that removes the
`Runtime.enable` / `Console.enable` CDP leaks — the deepest automation
detection signal, used by every major anti-bot (Cloudflare, DataDome, Google
Botguard). No amount of JS-layer spoofing (playwright-stealth,
poltergeist_fp.js) can cover it, because the leak fires at the protocol level
before page JS runs.

It is also the only driver whose browser build still passes Google's network
gate: rebrowser-playwright and vanilla playwright both ship a Chromium whose
TLS/HTTP-2 fingerprint gets every google.com request killed through the
NodeMaven tunnel (verified: 30s timeouts across all 10 sticky IPs, while the
same proxies return 200 to requests and to patchright's patched build). So
rebrowser is deliberately NOT the default despite its better init-script
support — it cannot reach Google at all.

Caveat accepted: patchright silently drops context-level add_init_script, so
the fingerprint mask in stealth_browser never runs. Compensations live in
launch args and context options instead (locale, timezone, WebRTC policy).

    PLAYWRIGHT_DRIVER=patchright  # default — CDP patches + Google reachable
    PLAYWRIGHT_DRIVER=playwright  # vanilla, init scripts work, google times out
    PLAYWRIGHT_DRIVER=rebrowser   # CDP + init scripts, google unreachable

Note: patchright patches Chromium only (we launch chromium in every engine
mode anyway) and disables the Console API, so page console listeners never
fire — this codebase does not rely on them.
"""

import logging
import os

logger = logging.getLogger('gmail_creator_pw')

_driver_pref = os.environ.get("PLAYWRIGHT_DRIVER", "patchright").strip().lower()
if _driver_pref not in ("rebrowser", "patchright", "playwright", "auto"):
    _driver_pref = "patchright"

async_playwright = None
driver_name = None

for _candidate, _module in (
    (_driver_pref, "patchright.async_api"),
    ("playwright", "playwright.async_api"),
):
    if _driver_pref != "auto" and _candidate != _driver_pref:
        continue
    try:
        _pw = __import__(_module, fromlist=["async_playwright"])
        async_playwright = _pw.async_playwright
        driver_name = _candidate
        break
    except ImportError:
        if _candidate == _driver_pref:
            logger.warning(
                "PLAYWRIGHT_DRIVER=%s is not installed; falling back. "
                "Install with: pip install patchright && patchright install chromium",
                _driver_pref,
            )

if async_playwright is None:
    from playwright.async_api import async_playwright as _pw
    async_playwright = _pw
    driver_name = "playwright"

logger.info(f"Playwright driver: {driver_name}")
