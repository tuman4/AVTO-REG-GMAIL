"""Playwright driver shim.

Patchright is a drop-in patched build of Playwright that removes the
`Runtime.enable` / `Console.enable` CDP leaks — the deepest automation
detection signal, used by every major anti-bot (Cloudflare, DataDome, etc.).
No amount of JS-layer spoofing (playwright-stealth, poltergeist_fp.js) can
cover it, because the leak fires at the protocol level before page JS runs.

This module resolves `async_playwright` from patchright when installed, and
silently falls back to vanilla playwright otherwise. Set

    PLAYWRIGHT_DRIVER=patchright   # default, requires `pip install patchright`
    PLAYWRIGHT_DRIVER=playwright   # vanilla

Note: patchright patches Chromium only (we launch chromium in every engine
mode anyway) and disables the Console API, so page console listeners never
fire — this codebase does not rely on them.
"""

import logging
import os

logger = logging.getLogger('gmail_creator_pw')

_driver_pref = os.environ.get("PLAYWRIGHT_DRIVER", "patchright").strip().lower()
if _driver_pref not in ("patchright", "playwright", "auto"):
    _driver_pref = "patchright"

async_playwright = None
driver_name = None

if _driver_pref in ("patchright", "auto"):
    try:
        from patchright.async_api import async_playwright as _pw
        async_playwright = _pw
        driver_name = "patchright"
    except ImportError:
        if _driver_pref == "patchright":
            logger.warning(
                "PLAYWRIGHT_DRIVER=patchright but `patchright` is not installed; "
                "falling back to vanilla playwright. "
                "Install with: pip install patchright && patchright install chromium"
            )

if async_playwright is None:
    from playwright.async_api import async_playwright as _pw
    async_playwright = _pw
    driver_name = "playwright"

logger.info(f"Playwright driver: {driver_name}")
