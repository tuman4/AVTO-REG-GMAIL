"""
Captcha Solver - Unified interface for captcha solving services (2Captcha, Anti-Captcha, CapMonster)

Blocking-safe: solve() and solve_async(). solve_async() offloads the synchronous
HTTP work to a worker thread so it never freezes the asyncio event loop.
"""
import time
import asyncio
import logging
import requests
from config.settings import Config

logger = logging.getLogger('gmail_creator_captcha')

# A single solve must never exceed this wall-clock budget, no matter how many
# services are configured — otherwise one call can stall a batch for 8+ minutes.
MAX_SOLVE_SECONDS = 180
_POLL_ROUNDS = 24          # 24 rounds x ~5s ≈ 120s max per service
_POLL_INTERVAL = 5
_INITIAL_WAIT = 12         # most solvers return within ~10s; poll early instead of sleeping blind


class CaptchaSolver:
    @staticmethod
    def solve(site_key, page_url):
        """Synchronous entry point. Returns the token or None."""
        deadline = time.time() + MAX_SOLVE_SECONDS

        services = []
        if Config.TWOCAPTCHA_API_KEY:
            services.append(("2captcha", CaptchaSolver._solve_2captcha))
        if Config.ANTICAPTCHA_API_KEY:
            services.append(("anticaptcha", CaptchaSolver._solve_anticaptcha))
        if Config.CAPMONSTER_API_KEY:
            services.append(("capmonster", CaptchaSolver._solve_capmonster))

        if not services:
            logger.warning("Captcha: no API key configured (2Captcha/Anti-Captcha/CapMonster)")
            return None

        for name, fn in services:
            if time.time() >= deadline:
                logger.warning(f"Captcha: overall budget exhausted before trying {name}")
                break
            try:
                token = fn(site_key, page_url, deadline)
                if token:
                    return token
                logger.warning(f"Captcha: {name} returned no token, trying next service")
            except Exception as e:
                logger.error(f"Captcha: {name} error: {e}")

        logger.error("Captcha: all configured services failed")
        return None

    @staticmethod
    async def solve_async(site_key, page_url):
        """Await this from async code — never blocks the event loop."""
        return await asyncio.to_thread(CaptchaSolver.solve, site_key, page_url)

    # ── shared poll loop ──────────────────────────────────────────────────────

    @staticmethod
    def _poll(label, fetch_result, deadline):
        """fetch_result() -> (state, token); state in {'ready','processing','failed'}."""
        time.sleep(min(_INITIAL_WAIT, max(0, deadline - time.time())))
        for _ in range(_POLL_ROUNDS):
            if time.time() >= deadline:
                logger.warning(f"Captcha: {label} exceeded its time budget")
                return None
            try:
                state, token = fetch_result()
            except Exception as e:
                logger.debug(f"Captcha: {label} poll error: {e}")
                state, token = "processing", None

            if state == "ready" and token:
                logger.info(f"Captcha: {label} solved")
                return token
            if state == "failed":
                logger.warning(f"Captcha: {label} reported failure")
                return None
            time.sleep(_POLL_INTERVAL)
        return None

    # ── 2Captcha ──────────────────────────────────────────────────────────────

    @staticmethod
    def _solve_2captcha(site_key, page_url, deadline=None):
        try:
            submit_resp = requests.post("https://2captcha.com/in.php", data={
                "key": Config.TWOCAPTCHA_API_KEY,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            }, timeout=30)

            if submit_resp.status_code != 200:
                logger.warning(f"2Captcha submit HTTP {submit_resp.status_code}")
                return None

            data = submit_resp.json()
            if data.get("status") != 1:
                logger.warning(f"2Captcha submit rejected: {data.get('request')}")
                return None

            captcha_id = data["request"]
            logger.info(f"2Captcha task submitted: {captcha_id}")

            def fetch():
                resp = requests.get("https://2captcha.com/res.php", params={
                    "key": Config.TWOCAPTCHA_API_KEY,
                    "action": "get",
                    "id": captcha_id,
                    "json": 1,
                }, timeout=10)
                if resp.status_code != 200:
                    return "processing", None
                result = resp.json()
                if result.get("status") == 1:
                    return "ready", result.get("request")
                req = str(result.get("request", ""))
                if "CAPCHA_NOT_READY" in req:
                    return "processing", None
                if "ERROR" in req or "WRONG" in req:
                    return "failed", None
                return "processing", None

            return CaptchaSolver._poll("2captcha", fetch, deadline or time.time() + MAX_SOLVE_SECONDS)

        except Exception as e:
            logger.error(f"2Captcha error: {e}")
        return None

    # ── Anti-Captcha ──────────────────────────────────────────────────────────

    @staticmethod
    def _solve_anticaptcha(site_key, page_url, deadline=None):
        try:
            create_resp = requests.post("https://api.anti-captcha.com/createTask", json={
                "clientKey": Config.ANTICAPTCHA_API_KEY,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            }, timeout=30)

            if create_resp.status_code != 200:
                logger.warning(f"Anti-Captcha submit HTTP {create_resp.status_code}")
                return None

            data = create_resp.json()
            if data.get("errorId") != 0:
                logger.warning(f"Anti-Captcha submit rejected: {data.get('errorDescription')}")
                return None

            task_id = data["taskId"]
            logger.info(f"Anti-Captcha task submitted: {task_id}")

            def fetch():
                resp = requests.post("https://api.anti-captcha.com/getTaskResult", json={
                    "clientKey": Config.ANTICAPTCHA_API_KEY,
                    "taskId": task_id,
                }, timeout=10)
                if resp.status_code != 200:
                    return "processing", None
                result = resp.json()
                if result.get("errorId") not in (0, None):
                    return "failed", None
                if result.get("status") == "ready":
                    return "ready", result.get("solution", {}).get("gRecaptchaResponse")
                return "processing", None

            return CaptchaSolver._poll("anticaptcha", fetch, deadline or time.time() + MAX_SOLVE_SECONDS)

        except Exception as e:
            logger.error(f"Anti-Captcha error: {e}")
        return None

    # ── CapMonster ────────────────────────────────────────────────────────────

    @staticmethod
    def _solve_capmonster(site_key, page_url, deadline=None):
        try:
            create_resp = requests.post("https://api.capmonster.cloud/createTask", json={
                "clientKey": Config.CAPMONSTER_API_KEY,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            }, timeout=30)

            if create_resp.status_code != 200:
                logger.warning(f"CapMonster submit HTTP {create_resp.status_code}")
                return None

            data = create_resp.json()
            if data.get("errorId") != 0:
                logger.warning(f"CapMonster submit rejected: {data.get('errorDescription')}")
                return None

            task_id = data["taskId"]
            logger.info(f"CapMonster task submitted: {task_id}")

            def fetch():
                resp = requests.post("https://api.capmonster.cloud/getTaskResult", json={
                    "clientKey": Config.CAPMONSTER_API_KEY,
                    "taskId": task_id,
                }, timeout=10)
                if resp.status_code != 200:
                    return "processing", None
                result = resp.json()
                if result.get("errorId") not in (0, None):
                    return "failed", None
                if result.get("status") == "ready":
                    return "ready", result.get("solution", {}).get("gRecaptchaResponse")
                return "processing", None

            return CaptchaSolver._poll("capmonster", fetch, deadline or time.time() + MAX_SOLVE_SECONDS)

        except Exception as e:
            logger.error(f"CapMonster error: {e}")
        return None
