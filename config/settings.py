"""Application configuration.

Every value is sourced from the environment (``.env`` for local runs, real
environment variables for CI/containers). Flags that no module ever reads
were removed — a config surface that lies about what it controls is worse
than no config at all.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("gmail_creator_config")


def _flag(name: str, default: str = "False") -> bool:
    """Read a boolean env var. Anything but ``true`` (case-insensitive) is False."""
    return os.getenv(name, default).strip().lower() == "true"


def _int(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` on malformed input."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    # ── Account identity ────────────────────────────────────────────────
    YOUR_BIRTHDAY = os.getenv("YOUR_BIRTHDAY", "2 4 1990")
    YOUR_GENDER = os.getenv("YOUR_GENDER", "1")  # 1=Male, 2=Female, 3=Other
    YOUR_PASSWORD = os.getenv("YOUR_PASSWORD", "")
    RECOVERY_EMAIL = os.getenv("RECOVERY_EMAIL", "")

    # ── SMS providers ───────────────────────────────────────────────────
    # 5sim uses country names ("usa"); SMS-Activate/OnlineSIM use numeric
    # codes (US = 187 / 1). Russia (0/7) is mass-banned by Google — avoid.
    FIVESIM_API_KEY = os.getenv("FIVESIM_API_KEY", "")
    FIVESIM_COUNTRY = os.getenv("FIVESIM_COUNTRY", "canada")
    FIVESIM_OPERATOR = os.getenv("FIVESIM_OPERATOR", "any")
    # "any" (default) = pick the operator with the best cost-per-delivered-code
    # from 5sim's public price feed (needs no key). A specific operator pins it.
    FIVESIM_AUTO_OPERATOR = _flag("FIVESIM_AUTO_OPERATOR", "True")
    # Ceiling on what auto-selection may spend; operators above this are ignored.
    FIVESIM_MAX_PRICE = float(os.getenv("FIVESIM_MAX_PRICE", "0.50") or 0.50)

    SMS_ACTIVATE_API_KEY = os.getenv("SMS_ACTIVATE_API_KEY", "")
    SMS_ACTIVATE_COUNTRY = os.getenv("SMS_ACTIVATE_COUNTRY", "187")

    ONLINESIM_API_KEY = os.getenv("ONLINESIM_API_KEY", "")
    ONLINESIM_COUNTRY = os.getenv("ONLINESIM_COUNTRY", "1")

    GETSMS_API_KEY = os.getenv("GETSMS_API_KEY", "")
    GETSMS_COUNTRY = os.getenv("GETSMS_COUNTRY", "us")

    # vak-sms.com — the only provider still accepting RU cards (via Fride.io,
    # 10% fee) and the only one with a public price feed that includes stock.
    # Unlike the others there is no API key: it uses email/password login with a
    # reCAPTCHA v2 token on signin, then a session JWT.
    VAKSMS_EMAIL = os.getenv("VAKSMS_EMAIL", "")
    VAKSMS_PASSWORD = os.getenv("VAKSMS_PASSWORD", "")
    # ISO-3166 alpha-2, lowercase: ca, gb, ph, br... must match the proxy geo.
    VAKSMS_COUNTRY = os.getenv("VAKSMS_COUNTRY", "ca")
    # "gl" = google.com on vak-sms (their service codes, not "go").
    VAKSMS_SERVICE = os.getenv("VAKSMS_SERVICE", "gl")
    # Ceiling for auto-select; Canada google sits at $0.08, UK at $0.07.
    VAKSMS_MAX_PRICE = float(os.getenv("VAKSMS_MAX_PRICE", "0.15") or 0.15)

    # ── CAPTCHA providers ───────────────────────────────────────────────
    TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")
    ANTICAPTCHA_API_KEY = os.getenv("ANTICAPTCHA_API_KEY", "")
    CAPMONSTER_API_KEY = os.getenv("CAPMONSTER_API_KEY", "")

    # ── Proxy ───────────────────────────────────────────────────────────
    ENABLE_PROXY = _flag("ENABLE_PROXY")
    PROXY_FILE = os.getenv("PROXY_FILE", "config/proxies.txt")

    # Mobile proxies rotate the egress IP on demand; point this at the
    # provider's rotation URL to get a fresh IP per account.
    MOBILE_PROXY_IP_CHANGE_URL = os.getenv("MOBILE_PROXY_IP_CHANGE_URL", "")
    PROXY_CHANGE_WAIT_TIME = _int("PROXY_CHANGE_WAIT_TIME", 10)

    # ── Browser & engine ────────────────────────────────────────────────
    # playwright = primary. appium = Android "golden method". selenium = legacy.
    ENGINE_MODE = os.getenv("ENGINE_MODE", "playwright").lower()
    HEADLESS_MODE = _flag("HEADLESS_MODE")
    BROWSER_TIMEOUT = _int("BROWSER_TIMEOUT", 30)
    PLAYWRIGHT_DRIVER = os.getenv("PLAYWRIGHT_DRIVER", "patchright").lower()

    # ── Anti-detection & behaviour ──────────────────────────────────────
    ENABLE_SESSION_WARMING = _flag("ENABLE_SESSION_WARMING", "True")
    ENABLE_FINGERPRINT_MASKING = _flag("ENABLE_FINGERPRINT_MASKING", "True")
    ENABLE_HUMAN_TYPING_ERRORS = _flag("ENABLE_HUMAN_TYPING_ERRORS", "True")
    ENABLE_MAC_ROTATION = _flag("ENABLE_MAC_ROTATION", "True")
    DELAY_BETWEEN_ACCOUNTS = _int("DELAY_BETWEEN_ACCOUNTS", 30)

    # Off by default: verify a clean run works first, then enable one at a
    # time. playwright-stealth is applied regardless of these flags.
    ENABLE_POLTERGEIST = _flag("ENABLE_POLTERGEIST")
    ENABLE_GHOST_TYPER = _flag("ENABLE_GHOST_TYPER")

    # ── Data paths ──────────────────────────────────────────────────────
    NAMES_FILE = os.getenv("NAMES_FILE", "data/names.txt")
    LOG_FILE = os.getenv("LOG_FILE", "data/gmail_creator.log")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    ENABLE_LOGGING = _flag("ENABLE_LOGGING", "True")

    # ── Notifications (optional) ────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Voice OTP server (optional) ─────────────────────────────────────
    # REQUIRED for /voice and /otp — an empty token means the server refuses
    # to start rather than exposing an unauthenticated endpoint.
    VOICE_SERVER_TOKEN = os.getenv("VOICE_SERVER_TOKEN", "")

    @classmethod
    def validate(cls) -> list[str]:
        """Surface insecure or unusable configuration at startup.

        Returns warnings only — never blocks startup. Destructive settings
        are the operator's call; our job is to make sure they are deliberate.
        """
        warnings = []

        if not cls.VOICE_SERVER_TOKEN:
            warnings.append(
                "VOICE_SERVER_TOKEN is not set in .env — the voice OTP server "
                "will refuse to start. Set a strong secret token."
            )

        if cls.YOUR_PASSWORD:
            warnings.append(
                "YOUR_PASSWORD is set — every account will share it. One leak "
                "compromises them all. Leave empty for per-account passwords."
            )

        sms_keys = (
            cls.FIVESIM_API_KEY,
            cls.SMS_ACTIVATE_API_KEY,
            cls.ONLINESIM_API_KEY,
            cls.GETSMS_API_KEY,
            cls.VAKSMS_EMAIL,
        )
        if not any(sms_keys):
            warnings.append(
                "No SMS API key configured — only the free phone bypass will "
                "be attempted. Add a key to .env for SMS verification."
            )
        if cls.VAKSMS_EMAIL and not cls.VAKSMS_PASSWORD:
            warnings.append(
                "VAKSMS_EMAIL is set but VAKSMS_PASSWORD is empty — the "
                "vak-sms provider will be skipped."
            )

        if cls.ENABLE_PROXY and not cls.PROXY_FILE:
            warnings.append("ENABLE_PROXY is set but PROXY_FILE is empty.")

        for warning in warnings:
            logger.warning(warning)

        return warnings
