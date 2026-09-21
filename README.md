# Gmail Infinity

**Automated Gmail account creation for security research and testing.**

Three automation engines — Playwright (primary), Selenium (legacy), and Appium
on Android emulators (the "golden method") — with pluggable proxy rotation,
four SMS verification providers, captcha solving, and encrypted credential
storage.

---

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [How registration actually works](#how-registration-actually-works)
- [Security](#security)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)

---

## Overview

This tool creates Gmail accounts programmatically. It does not exploit a
vulnerability — it drives the same public signup flow a human would, just
faster and with more consistent fingerprints. Whether it *succeeds* depends
almost entirely on signals Google scores at the network layer, which is why
proxy quality is the single highest-leverage configuration decision.

**What it is good for:** penetration-testing mail flows, building throwaway
accounts for a test suite, studying anti-bot detection.

**What it is not good for:** anything that violates Google's Terms of Service.

## Architecture

```
auto_gmail_creator.py     Entry point: rich TUI, menu, batch orchestration
config/settings.py        Single Config class, every value from environment
core/
  pw.py                   Driver shim: patchright > vanilla playwright
  stealth_browser.py      Playwright manager: fingerprint, warmup, injection
  selenium_runner.py      Selenium engine + CDP fingerprint injection
  android_creator.py      Appium engine (Android native settings flow)
  runners.py              Flow functions wiring engines to batch logic
  batch_runner.py         Threaded batch orchestrator
  fingerprint.py          JS payload loader for Selenium CDP injection
  behavior.py             Bézier mouse curves, human-like delays
  proxy_manager.py        Proxy pool: parse / rotate / blacklist / health
  phone_bypass.py         Phone-challenge strategies (skip, email, SMS API)
  captcha_solver.py       2captcha / anti-captcha / capsolver
  sms_manager.py          5sim / SMS-Activate / OnlineSIM / GetSMS
  trust_builder.py        Pre-signup trust warming
  warmup.py               In-session browsing warmup
  account_warmer.py       Post-creation account aging
  secure_vault.py         Fernet encryption for stored credentials
  database.py             SQLite (WAL) + JSON persistence, vault-backed
  account_manager.py      Account CRUD
  retry_engine.py         Failure attribution + backoff
  session_resume.py       Resume an interrupted batch
  config_validator.py     Pre-flight config sanity check
  health_checker.py       IMAP health check for created accounts
  telegram_notifier.py    Notifications, secrets masked in output
  ui.py                   TUI panels, progress, live system status
js/
  poltergeist_fp.js       Fingerprint spoof (session-frozen values)
  ghost_typer.js          Human-like typing and pointer events
services/
  voice.py                Optional local voice-OTP HTTP server
data/                     Runtime state: DB, vault key, logs, name pool
```

## Quick start

```bash
git clone https://github.com/<owner>/gmail-infinity.git
cd gmail-infinity

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
patchright install chromium     # patched driver browser
playwright install chromium     # fallback browser

copy .env.example .env          # then edit .env
python auto_gmail_creator.py
```

The validator runs first and reports exactly what is missing or misconfigured
before anything launches.

### Minimum viable config

**You need proxies.** Without them registration almost always hits phone
verification, and from a residential IP you will burn that IP's quota fast.

`config/proxies.txt` — one per line:

```
host:port
host:port:user:pass
user:pass@host:port
socks5://host:port
[2001:db8::1]:8080
```

`.env`:

```ini
ENGINE_MODE=playwright
HEADLESS_MODE=False
ENABLE_PROXY=True
```

Then add one SMS provider if you want automatic phone verification:

```ini
FIVESIM_API_KEY=...
FIVESIM_COUNTRY=usa
```

## Configuration

Everything lives in `.env`; `config/settings.py` reads it into a single
`Config` class. There are no config flags that do nothing — dead switches
were removed, because a config surface that lies about what it controls is
worse than no config at all.

| Group | Keys | Notes |
|---|---|---|
| Engine | `ENGINE_MODE`, `HEADLESS_MODE`, `BROWSER_TIMEOUT`, `PLAYWRIGHT_DRIVER` | `playwright` recommended |
| Identity | `YOUR_BIRTHDAY`, `YOUR_GENDER`, `YOUR_PASSWORD`, `RECOVERY_EMAIL` | Empty password = unique per account |
| Proxy | `ENABLE_PROXY`, `DELAY_BETWEEN_ACCOUNTS`, `MOBILE_PROXY_IP_CHANGE_URL` | Proxies in `config/proxies.txt` |
| SMS | `FIVESIM_*`, `SMS_ACTIVATE_*`, `ONLINESIM_*`, `GETSMS_*` | Any one is enough |
| Captcha | `TWOCAPTCHA_*`, `ANTICAPTCHA_*`, `CAPMONSTER_*` | Optional |
| Stealth | `ENABLE_POLTERGEIST`, `ENABLE_GHOST_TYPER`, `ENABLE_MAC_ROTATION` | Off until a clean run works |
| Logging | `ENABLE_LOGGING`, `LOG_FILE`, `LOG_LEVEL` | |
| Notifiers | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `VOICE_SERVER_TOKEN` | Optional |

### Playwright driver

`PLAYWRIGHT_DRIVER=patchright` (default) uses
[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), a drop-in
patched Playwright that closes the `Runtime.enable` and `Console.enable` CDP
leaks. These are protocol-level signals that every major anti-bot vendor
fingerprints, and **no amount of JavaScript spoofing can close them** — the
leak fires before page scripts run. `core/pw.py` prefers patchright and falls
back to vanilla `playwright` if it is absent.

Set `PLAYWRIGHT_DRIVER=playwright` to force vanilla.

## How registration actually works

1. **Pre-flight** — `config_validator.py` checks keys, proxy file parseability,
   and driver state.
2. **Warmup** — browse Google properties to build session trust before
   touching the signup URL.
3. **Signup flow** — fill name, birthday, username, password. Selectors are
   tried in priority order with locale fallbacks (English/Arabic).
4. **Phone challenge** — try skip, then email-instead, then SMS API purchase.
5. **Persist** — credentials encrypted via Fernet, stored in SQLite + JSON.
6. **Age** — optional post-creation warming (YouTube, Mail, Search).

Success probability is roughly **60% IP reputation, 30% SMS verification,
10% browser fingerprint** — in that order. Spending on a better proxy pool
buys more than any amount of stealth code.

## Security

- **Credentials are encrypted at rest.** Passwords are Fernet-encrypted in
  `data/.vault.key`-sealed form. The key is created with `0600` permissions.
- **`.gitignore` excludes** `.env`, `data/`, `config/proxies.txt`.
- **Do not commit `.env`, `data/`, or the vault key.** Losing the key loses
  every stored password — back it up separately or not at all.
- **`VOICE_SERVER_TOKEN` is mandatory** — the voice server refuses to start
  without it rather than expose an unauthenticated endpoint.
- **Telegram notifications mask passwords and proxy credentials.**
- The original build stored 11 accounts in plaintext. The first run migrates
  them automatically; a backup is written to `data/database.db.bak`.

## Known limitations

- **Phone bypass is not guaranteed.** Google decides based on IP and device
  reputation. If it demands a number, you need an SMS provider — there is no
  code-level workaround.
- **reCAPTCHA v3 invisible** is the hardest blocker. Solvers exist but are
  expensive and unreliable against v3.
- **Patchright is Chromium-only.** Firefox and WebKit engines are unaffected.
- **Appium requires a running emulator + Appium server** on port 4723.
- **Cookie import/export was removed** — Google rejects re-signed session
  MACs, so the feature could never work and only added attack surface.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Proxy file ... is empty` | Add proxies to `config/proxies.txt` |
| `Phone verification is strictly required` | IP reputation — switch to mobile/residential proxies + SMS key |
| `2captcha: ERROR_*` | Low balance or bad key — check `check_balance` output |
| `playwright` import fails | `pip install playwright && playwright install chromium` |
| Appium won't connect | Start emulator + `appium` server, confirm port 4723 |
| `cryptography` ImportError | `pip install cryptography>=42.0.0` |
| `RuntimeError: ... asyncio` | Re-run; the event loop teardown is racy on Ctrl-C |

---

## Disclaimer

Provided for authorized security testing, research, and educational use
only. Automating account creation violates Google's Terms of Service; using
created accounts for spam, fraud, or abuse is illegal in most jurisdictions.
The authors accept no responsibility for misuse. **Do not use this to harm
people.**
