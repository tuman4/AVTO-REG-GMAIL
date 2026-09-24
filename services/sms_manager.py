"""
SMS Manager - Unified async SMS service handler
Provides a single interface for all SMS services:
  - 5sim.net (with cancel/finish/balance)
  - SMS-Activate
  - OnlineSIM
  - GetSMS
"""
import asyncio
import base64
import json
import re
import time
import logging
import aiohttp
from config.settings import Config

logger = logging.getLogger('gmail_creator_sms')

MAX_POLL_SECONDS = 180
POLL_INTERVAL = 5

# Google verification codes are 6 digits; accept 4-8 to cover rare variants, but
# never match a number embedded in other text (prices, short codes, phone numbers).
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _extract_code(text: str):
    """Pull the verification code out of an SMS body or a STATUS_OK:code response."""
    if not text:
        return None
    text = str(text).strip()

    # "STATUS_OK:123456" style — take the payload after the first colon
    if ":" in text:
        head, _, payload = text.partition(":")
        payload = payload.strip()
        if payload and payload.isdigit():
            return payload

    # Free-form SMS body — find a standalone digit run that looks like an OTP,
    # ignoring anything preceded or followed by other digits.
    match = _CODE_RE.search(text)
    if match:
        return match.group(1)

    # Spelled-out digits ("one two three four five six")
    words = re.findall(r"\b(one|two|three|four|five|six|seven|eight|nine|zero)\b", text.lower())
    if len(words) >= 4:
        digit_map = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                     "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
        return "".join(digit_map[w] for w in words[:8])

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API (async)
# ══════════════════════════════════════════════════════════════════════════════

async def get_phone_from_any_service():
    """
    Try to get a phone number from any configured SMS service.
    Returns: {'phone': str, 'id': str, 'service': str} or None
    """
    services = [
        ("5sim",         Config.FIVESIM_API_KEY,       _get_5sim_phone),
        ("sms_activate", Config.SMS_ACTIVATE_API_KEY,  _get_sms_activate_phone),
        ("onlinesim",    Config.ONLINESIM_API_KEY,     _get_onlinesim_phone),
        ("getsms",       Config.GETSMS_API_KEY,        _get_getsms_phone),
        ("vaksms",       Config.VAKSMS_EMAIL,          _get_vaksms_phone),
    ]

    for name, api_key, get_fn in services:
        if not api_key:
            continue
        try:
            logger.info(f"Trying SMS service: {name}")
            result = await get_fn()
            if result:
                result['service'] = name
                logger.info(f"Got phone from {name}: {result['phone']}")
                return result
        except Exception as e:
            logger.warning(f"{name} failed: {e}")

    logger.error("No SMS service available or all failed.")
    return None


async def get_code_from_service(service_name: str, order_id: str, wait_time: int = MAX_POLL_SECONDS):
    """
    Wait for and retrieve the SMS verification code.
    Returns: str (code) or None
    """
    poll_fn = {
        '5sim':         _poll_5sim_code,
        'sms_activate': _poll_sms_activate_code,
        'onlinesim':    _poll_onlinesim_code,
        'getsms':       _poll_getsms_code,
        'vaksms':       _poll_vaksms_code,
    }.get(service_name)

    if not poll_fn:
        logger.error(f"Unknown SMS service: {service_name}")
        return None

    return await poll_fn(order_id, wait_time)


async def cancel_order(service_name: str, order_id: str):
    """Cancel an SMS order (saves money when verification fails)."""
    try:
        cancel_fn = {
            '5sim':         _cancel_5sim_order,
            'sms_activate': _cancel_sms_activate_order,
            'onlinesim':    _cancel_onlinesim_order,
            'getsms':       _cancel_getsms_order,
            'vaksms':       _cancel_vaksms_order,
        }.get(service_name)

        if cancel_fn:
            await cancel_fn(order_id)
            logger.info(f"Order {order_id} cancelled on {service_name}")
    except Exception as e:
        logger.warning(f"Failed to cancel order {order_id} on {service_name}: {e}")


async def finish_order(service_name: str, order_id: str):
    """Mark an SMS order as completed (after successful verification)."""
    try:
        finish_fn = {
            '5sim':         _finish_5sim_order,
            'sms_activate': _finish_sms_activate_order,
            'onlinesim':    None,
            'getsms':       _finish_getsms_order,
            'vaksms':       _finish_vaksms_order,
        }.get(service_name)

        if finish_fn:
            await finish_fn(order_id)
            logger.info(f"Order {order_id} finished on {service_name}")
    except Exception as e:
        logger.warning(f"Failed to finish order {order_id} on {service_name}: {e}")


async def check_balance(service_name: str = None):
    """Check balance for a specific service or all configured services."""
    results = {}
    services = {
        '5sim': (Config.FIVESIM_API_KEY, _get_5sim_balance),
        'sms_activate': (Config.SMS_ACTIVATE_API_KEY, _get_sms_activate_balance),
        'onlinesim': (getattr(Config, 'ONLINESIM_API_KEY', ''), _get_onlinesim_balance),
        'getsms': (getattr(Config, 'GETSMS_API_KEY', ''), _get_getsms_balance),
        'vaksms': (Config.VAKSMS_EMAIL, _get_vaksms_balance),
    }

    if service_name:
        key, fn = services.get(service_name, (None, None))
        if not key:
            logger.warning(f"check_balance: {service_name} has no API key configured")
        elif fn:
            try:
                results[service_name] = await fn()
            except Exception as e:
                logger.warning(f"{service_name} balance check failed: {e}")
                results[service_name] = None
    else:
        for name, (key, fn) in services.items():
            if not key:
                continue
            try:
                results[name] = await fn()
            except Exception as e:
                logger.warning(f"{name} balance check failed: {e}")
                results[name] = None

    return results


def format_phone_for_google(phone: str) -> str:
    """
    Format phone number for Google's input field.
    Google expects an E.164-style number: digits with an optional leading '+'.
    """
    if not phone:
        return phone
    phone = phone.strip()
    digits = re.sub(r"\D", "", phone)
    # national numbers (no country code) — assume a US-style 10-digit number
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}"


# ═════════════════════════════════════════════════════════════════════════════
# 5sim operator auto-selection
# ═══════════════════════════════════════════════════════════════════════════════

# Public price feed — no token needed. 5sim's getNumbersStatus reports raw
# stock per operator; this one also carries real 24h delivery rates, which is
# what separates "cheap" from "cheap and actually delivers".
_5SIM_PRICES_URL = "https://5sim.net/v1/guest/prices?product=google"

# Cached choice: prices barely move inside a single batch run, but the API is
# a round trip we don't want on every account.
_selection = {"expires": 0.0, "country": None, "operator": None}
_SELECTION_TTL = 3600  # one hour


def _cost_per_success(cost: float, rate: float) -> float:
    """Effective price per delivered code. Providers charge per attempt, so a
    $0.08 number that lands 3% of the time costs more than a $0.20 at 60%."""
    return cost / (rate / 100.0) if rate > 0 else float("inf")


async def _fetch_5sim_prices(country: str) -> dict:
    """Return {operator: {cost, rate24}} for google numbers in `country`."""
    async with aiohttp.ClientSession() as session:
        async with session.get(_5SIM_PRICES_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"5sim price feed returned {resp.status}")
                return {}
            data = await resp.json()

    # Shape is product-first: {"google": {"<country>": {"<operator>": {...}}}}
    product = data.get("google", {})
    return product.get(country, {})


async def _select_5sim_operator(country: str, max_price: float) -> tuple:
    """Pick the operator with the best cost-per-successful-delivery.

    Returns (operator, cost) or (None, None) when the feed is unreachable or
    no operator meets the price floor — the caller falls back to the env value.
    """
    try:
        offers = await _fetch_5sim_prices(country)
    except Exception as e:
        logger.warning(f"5sim price feed unreachable: {e}")
        return None, None

    if not offers:
        logger.warning(f"5sim price feed has no google offers for '{country}'")
        return None, None

    best_op, best_cps, best_cost = None, float("inf"), None
    for operator, offer in offers.items():
        if not isinstance(offer, dict):
            continue
        cost = offer.get("cost")
        count = offer.get("count", 0)
        if cost is None or not count:
            continue
        rate = offer.get("rate24") or offer.get("rate") or 0
        if cost > max_price:
            continue
        cps = _cost_per_success(cost, rate)
        if cps < best_cps:
            best_op, best_cps, best_cost = operator, cps, cost

    if not best_op:
        logger.warning(
            f"No 5sim operator under ${max_price:.2f} for '{country}' "
            f"({len(offers)} offers, all above price floor)"
        )
        return None, None

    rate = offers[best_op].get("rate24") or offers[best_op].get("rate") or 0
    logger.info(
        f"5sim auto-pick for '{country}': {best_op} @ ${best_cost:.4f} "
        f"(24h delivery {rate:.2f}%, cost/success ${best_cps:.4f})"
    )
    return best_op, best_cost


async def _resolve_5sim_operator() -> str:
    """The operator to buy from: auto-selected, or the env default."""
    country = getattr(Config, 'FIVESIM_COUNTRY', 'usa')
    configured = getattr(Config, 'FIVESIM_OPERATOR', 'any')
    enabled = getattr(Config, 'FIVESIM_AUTO_OPERATOR', True)

    if not enabled or configured != "any":
        return configured

    now = time.time()
    if _selection["operator"] and now < _selection["expires"] and _selection["country"] == country:
        return _selection["operator"]

    max_price = getattr(Config, 'FIVESIM_MAX_PRICE', 0.50)
    operator, _ = await _select_5sim_operator(country, max_price)
    if not operator:
        return configured

    _selection.update(
        expires=now + _SELECTION_TTL,
        country=country,
        operator=operator,
    )
    return operator


# ═════════════════════════════════════════════════════════════════════════════
# 5sim implementation
# ══════════════════════════════════════════════════════════════════════════════

def _5sim_headers():
    return {"Authorization": f"Bearer {Config.FIVESIM_API_KEY}", "Accept": "application/json"}


async def _get_5sim_balance():
    url = "https://5sim.net/v1/user/profile"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                balance = data.get("balance", 0)
                logger.info(f"5sim balance: {balance}")
                return balance
    return None


async def _get_5sim_phone():
    balance = await _get_5sim_balance()
    if balance is not None and balance < 1:
        logger.error(f"5sim balance too low: {balance}")
        return None

    country = getattr(Config, 'FIVESIM_COUNTRY', 'usa')
    operator = await _resolve_5sim_operator()
    url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/google"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"5sim buy failed ({resp.status}): {text}")
                return None
            data = await resp.json()

    phone = data.get("phone", "")
    order_id = str(data.get("id", ""))
    if phone and order_id:
        return {"phone": phone, "id": order_id}
    return None


async def _poll_5sim_code(order_id: str, wait_time: int):
    url = f"https://5sim.net/v1/user/check/{order_id}"
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        status = data.get("status", "")
                        if status == "CANCELED":
                            logger.warning("5sim: order was cancelled")
                            return None
                        sms_list = data.get("sms", [])
                        if sms_list:
                            raw_code = sms_list[0].get("code")
                            if raw_code:
                                code = _extract_code(str(raw_code))
                                if not code:
                                    logger.warning(f"5sim: could not parse code from SMS body: {raw_code!r}")
                                    return None
                                logger.info(f"5sim code received: {code}")
                                return code
            except Exception as e:
                logger.warning(f"5sim poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("5sim: timed out waiting for code")
    return None


async def _cancel_5sim_order(order_id: str):
    url = f"https://5sim.net/v1/user/cancel/{order_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                logger.info(f"5sim order {order_id} cancelled")
            else:
                text = await resp.text()
                logger.warning(f"5sim cancel failed ({resp.status}): {text}")


async def _finish_5sim_order(order_id: str):
    url = f"https://5sim.net/v1/user/finish/{order_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                logger.info(f"5sim order {order_id} finished")


# ══════════════════════════════════════════════════════════════════════════════
# SMS-Activate implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_sms_activate_balance():
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "getBalance"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = (await resp.text()).strip()
            if text.startswith("ACCESS_BALANCE:"):
                balance = float(text.split(":")[1])
                logger.info(f"SMS-Activate balance: {balance}")
                return balance
    return None


async def _get_sms_activate_phone():
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {
        "api_key": Config.SMS_ACTIVATE_API_KEY,
        "action": "getNumber",
        "service": "go",
        "country": Config.SMS_ACTIVATE_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = (await resp.text()).strip()

    if text.startswith("ACCESS_NUMBER"):
        parts = text.split(":")
        if len(parts) >= 3:
            return {"phone": parts[2], "id": parts[1]}
    elif "NO_NUMBERS" in text:
        logger.warning("SMS-Activate: no numbers available")
    elif "NO_BALANCE" in text:
        logger.error("SMS-Activate: insufficient balance")
    else:
        logger.warning(f"SMS-Activate response: {text}")
    return None


async def _poll_sms_activate_code(order_id: str, wait_time: int):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {
        "api_key": Config.SMS_ACTIVATE_API_KEY,
        "action": "getStatus",
        "id": order_id,
    }
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = (await resp.text()).strip()
                    if text.startswith("STATUS_OK"):
                        code = _extract_code(text)
                        if not code:
                            logger.warning(f"SMS-Activate: unparseable STATUS_OK response: {text!r}")
                            return None
                        logger.info(f"SMS-Activate code received: {code}")
                        return code
                    elif text == "STATUS_CANCEL":
                        logger.warning("SMS-Activate: order cancelled")
                        return None
            except Exception as e:
                logger.warning(f"SMS-Activate poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("SMS-Activate: timed out waiting for code")
    return None


async def _cancel_sms_activate_order(order_id: str):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "setStatus", "id": order_id, "status": 8}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = (await resp.text()).strip()
            logger.info(f"SMS-Activate cancel response: {text}")


async def _finish_sms_activate_order(order_id: str):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "setStatus", "id": order_id, "status": 6}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = (await resp.text()).strip()
            logger.info(f"SMS-Activate finish response: {text}")


# ══════════════════════════════════════════════════════════════════════════════
# OnlineSIM implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_onlinesim_balance():
    url = "https://onlinesim.io/api/getBalance.php"
    params = {"apikey": Config.ONLINESIM_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                balance = data.get("balance")
                if balance is not None:
                    logger.info(f"OnlineSIM balance: {balance}")
                    return float(balance)
    except Exception as e:
        logger.warning(f"OnlineSIM balance check failed: {e}")
    return None


async def _get_onlinesim_phone():
    url = "https://onlinesim.io/api/getNum.php"
    params = {
        "apikey": Config.ONLINESIM_API_KEY,
        "service": "Google",
        "country": Config.ONLINESIM_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()

        if data.get("response") == 1:
            tzid = str(data.get("tzid", ""))
            if tzid:
                state_url = "https://onlinesim.io/api/getState.php"
                state_params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": tzid}
                for _ in range(10):
                    try:
                        async with session.get(state_url, params=state_params, timeout=aiohttp.ClientTimeout(total=10)) as sr:
                            state_data = await sr.json()
                            if isinstance(state_data, list) and len(state_data) > 0:
                                item = state_data[0]
                                if item.get("number"):
                                    return {"phone": item["number"], "id": tzid}
                    except Exception:
                        pass
                    await asyncio.sleep(3)
        else:
            logger.warning(f"OnlineSIM getNum response: {data}")
    return None


async def _poll_onlinesim_code(order_id: str, wait_time: int):
    url = "https://onlinesim.io/api/getState.php"
    params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": order_id}
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        msg = data[0].get("msg", "")
                        if msg:
                            code = _extract_code(msg)
                            if code:
                                logger.info(f"OnlineSIM code received: {code}")
                                return code
            except Exception as e:
                logger.warning(f"OnlineSIM poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("OnlineSIM: timed out waiting for code")
    return None


async def _cancel_onlinesim_order(order_id: str):
    url = "https://onlinesim.io/api/setOperationRevise.php"
    params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": order_id}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            logger.info(f"OnlineSIM cancel response: {resp.status}")


# ══════════════════════════════════════════════════════════════════════════════
# GetSMS implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_getsms_balance():
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {"api_key": Config.GETSMS_API_KEY, "action": "getBalance"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = (await resp.text()).strip()
                if text.startswith("ACCESS_BALANCE:"):
                    return float(text.split(":")[1])
    except Exception as e:
        logger.warning(f"GetSMS balance check failed: {e}")
    return None


async def _get_getsms_phone():
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {
        "api_key": Config.GETSMS_API_KEY,
        "action": "getNumber",
        "service": "go",
        "country": Config.GETSMS_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = (await resp.text()).strip()

    if text.startswith("ACCESS_NUMBER"):
        parts = text.split(":")
        if len(parts) >= 3:
            return {"phone": parts[2], "id": parts[1]}

    logger.warning(f"GetSMS getNumber response: {text}")
    return None


async def _poll_getsms_code(order_id: str, wait_time: int):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {
        "api_key": Config.GETSMS_API_KEY,
        "action": "getStatus",
        "id": order_id,
    }
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = (await resp.text()).strip()
                    if text.startswith("STATUS_OK"):
                        code = _extract_code(text)
                        if not code:
                            logger.warning(f"GetSMS: unparseable STATUS_OK response: {text!r}")
                            return None
                        logger.info(f"GetSMS code received: {code}")
                        return code
                    elif text == "STATUS_CANCEL":
                        return None
            except Exception as e:
                logger.warning(f"GetSMS poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("GetSMS: timed out waiting for code")
    return None


async def _cancel_getsms_order(order_id: str):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {"api_key": Config.GETSMS_API_KEY, "action": "setStatus", "id": order_id, "status": 8}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            logger.info(f"GetSMS cancel response: {resp.status}")


async def _finish_getsms_order(order_id: str):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {"api_key": Config.GETSMS_API_KEY, "action": "setStatus", "id": order_id, "status": 6}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            logger.info(f"GetSMS finish response: {resp.status}")


# ══════════════════════════════════════════════════════════════════════════════
# vak-sms implementation
# ══════════════════════════════════════════════════════════════════════════════
# vak-sms has no API key. Signin takes email+password plus a reCAPTCHA v2 token,
# returns an {accessToken, ...} JWT bundle, and every later call carries
# Authorization: Bearer <token>. The token is decoded for the refresh path.
#
# Endpoints (read from their SPA bundle, not guessed):
#   POST /auth/signin            {username, password, captcha}
#   POST /auth/refresh           (cookie/Bearer)  -> new accessToken
#   GET  /user/current/me                        -> {apiKey, balance, ...}
#   GET  /country/stats?serviceId=<svc>          -> price tiers per country
#   POST /number/buy   {countryCode, operatorId, serviceCode, rentTime, price}
#   GET  /number/active?page=1&count=50          -> active orders incl. smsCode
#   POST /number/cancel {phoneNumber, isBanned, serviceCode}
#
# The buy response is {"tel": "+1..."}. The SPA's order id is the phone itself:
# /number/active, /number/cancel and /number/ban are all keyed on phoneNumber,
# not on a purchase id (purchaseId only surfaces in history responses).

VAKSMS_BASE = "https://vak-sms.com/backend"
VAKSMS_RECAPTCHA_SITEKEY = "6LdfAV4qAAAAAKmrKRCHYNdNa2aNKl2FNxrqo_Og"
_VAKSMS_TOKEN = None       # cached JWT
_VAKSMS_EXPIRES = 0.0      # unix ts when it goes stale
_VAKSMS_LAST_PRICE = None  # (tier price, ts) — refreshed hourly
_VAKSMS_PRICE_TTL = 3600


def _jwt_exp(token: str) -> float:
    """exp claim from a JWT, 0.0 when the payload has no exp."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return 0.0


async def _vaksms_signin() -> str:
    """Solve the signin reCAPTCHA once, POST credentials, cache the JWT.

    Raises on failure — callers treat an exception as 'this service is down'.
    """
    from core.captcha_solver import CaptchaSolver

    captcha = await CaptchaSolver.solve_async(
        VAKSMS_RECAPTCHA_SITEKEY, "https://vak-sms.com/signin"
    )
    if not captcha:
        raise RuntimeError("vak-sms: could not solve signin reCAPTCHA")

    payload = {
        "username": Config.VAKSMS_EMAIL,
        "password": Config.VAKSMS_PASSWORD,
        "captcha": captcha,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{VAKSMS_BASE}/auth/signin", json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            # NestJS returns 201 Created on a fresh session; treat 200/201 alike.
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"vak-sms signin failed ({resp.status}): {text[:200]}")
            data = await resp.json()

    token = data.get("accessToken") or ""
    if not token:
        raise RuntimeError(f"vak-sms signin returned no accessToken: {data}")
    _set_vaksms_token(token)
    logger.info("vak-sms: signed in, token cached")
    return token


def _set_vaksms_token(token: str):
    global _VAKSMS_TOKEN, _VAKSMS_EXPIRES
    exp = _jwt_exp(token)
    # exp is the server's hard limit; keep a margin for clock drift.
    _VAKSMS_EXPIRES = (exp - 60) if exp else (time.time() + 3600)
    _VAKSMS_TOKEN = token


async def _vaksms_token() -> str:
    """A live JWT for this session, refreshing when the current one is stale."""
    if _VAKSMS_TOKEN and time.time() < _VAKSMS_EXPIRES:
        return _VAKSMS_TOKEN

    if _VAKSMS_TOKEN:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{VAKSMS_BASE}/auth/refresh",
                headers={"Authorization": f"Bearer {_VAKSMS_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("accessToken") or ""
                    if token:
                        _set_vaksms_token(token)
                        logger.info("vak-sms: token refreshed")
                        return token
                logger.warning(f"vak-sms refresh failed ({resp.status}); signing in fresh")

    return await _vaksms_signin()


async def _vaksms_headers() -> dict:
    token = await _vaksms_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _vaksms_best_price() -> float:
    """Cheapest in-stock tier for the configured country+service.

    The public /country/stats feed carries real stock per price tier, so the
    adapter buys at the floor without ever guessing an operator — the backend
    assigns one when operatorId is omitted.
    """
    global _VAKSMS_LAST_PRICE
    now = time.time()
    if _VAKSMS_LAST_PRICE and now < _VAKSMS_LAST_PRICE[1]:
        return _VAKSMS_LAST_PRICE[0]

    service = getattr(Config, "VAKSMS_SERVICE", "gl")
    country = getattr(Config, "VAKSMS_COUNTRY", "ca")
    ceiling = getattr(Config, "VAKSMS_MAX_PRICE", 0.15)

    url = f"{VAKSMS_BASE}/country/stats"
    params = {"serviceId": service}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"vak-sms price feed returned {resp.status}")
            data = await resp.json()

    best_price = None
    for entry in data:
        if entry.get("id") != country:
            continue
        for tier in entry.get("available", []):
            if not tier.get("count"):
                continue
            price = tier.get("price")
            if price is None or price > ceiling:
                continue
            if best_price is None or price < best_price:
                best_price = price
        break

    if best_price is None:
        raise RuntimeError(
            f"vak-sms: no in-stock tier for country='{country}' service='{service}' "
            f"under ${ceiling:.2f}"
        )

    _VAKSMS_LAST_PRICE = (best_price, now + _VAKSMS_PRICE_TTL)
    logger.info(f"vak-sms: selected tier ${best_price:.4f} ({country}/{service})")
    return best_price


async def _get_vaksms_balance():
    headers = await _vaksms_headers()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{VAKSMS_BASE}/user/current/me", headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"vak-sms balance check failed ({resp.status})")
                return None
            data = await resp.json()
    balance = data.get("balance")
    if balance is None:
        return None
    return float(balance)


async def _get_vaksms_phone():
    country = getattr(Config, "VAKSMS_COUNTRY", "ca")
    service = getattr(Config, "VAKSMS_SERVICE", "gl")

    try:
        price = await _vaksms_best_price()
    except Exception as e:
        logger.error(f"vak-sms: {e}")
        return None

    payload = {
        "countryCode": country,
        "serviceCode": service,
        "rentTime": 1200,           # 20 minutes, same as the site default
        "price": price,
        "apiKind": "SITE",
    }
    headers = await _vaksms_headers()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{VAKSMS_BASE}/number/buy", json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            text = await resp.text()
            # 201 Created is the success code for a fresh rental; accepting only
            # 200 meant every real purchase logged a failure while the number
            # was already billed.
            if resp.status not in (200, 201):
                logger.error(f"vak-sms buy failed ({resp.status}): {text[:200]}")
                return None
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.error(f"vak-sms buy: unparseable response: {text[:200]}")
                return None

    phone = data.get("tel") or data.get("phone") or ""
    if not phone:
        logger.error(f"vak-sms buy returned no number: {data}")
        return None

    # vak-sms keys every later call by phone number, so it IS the order id here.
    # purchaseId is kept for support/refund lookups.
    return {"phone": phone, "id": phone,
            "purchase_id": data.get("purchaseId") or data.get("purchase_id")}


async def _vaksms_active_order(session, phone: str):
    """The active-order record for `phone`, or None when it is gone."""
    headers = await _vaksms_headers()
    async with session.get(
        f"{VAKSMS_BASE}/number/active",
        params={"page": 1, "count": 50},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            logger.warning(f"vak-sms active list returned {resp.status}")
            return None
        data = await resp.json()

    for item in (data.get("data") or []) if isinstance(data, dict) else []:
        # telNumber is the canonical E.164 form the backend returns from /buy.
        if str(item.get("telNumber") or "") == phone or str(item.get("tel") or "") == phone:
            return item
    return None


async def _poll_vaksms_code(order_id: str, wait_time: int):
    deadline = asyncio.get_running_loop().time() + wait_time
    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                item = await _vaksms_active_order(session, order_id)
                if not item:
                    # Number vanished without a code — cancelled or expired.
                    logger.warning(f"vak-sms: order {order_id} left the active list")
                    return None
                status = item.get("lastStatus")
                if status == "SmsReceived" or item.get("smsCode"):
                    raw = item.get("smsCode")
                    code = _extract_code(str(raw)) if raw else None
                    if code:
                        logger.info(f"vak-sms code received: {code}")
                        return code
                    logger.warning(f"vak-sms: could not parse code from: {raw!r}")
                    return None
                if status in ("CancelledByTimeout", "CancelByUserSuccess", "CancelBannedSuccess"):
                    logger.warning(f"vak-sms: order {order_id} cancelled (status={status})")
                    return None
            except Exception as e:
                logger.warning(f"vak-sms poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("vak-sms: timed out waiting for code")
    return None


async def _cancel_vaksms_order(order_id: str):
    # Phone numbers that already received a code cannot be refunded; only cancel
    # orders still waiting, otherwise the API bills the number anyway.
    payload = {
        "phoneNumber": order_id,
        "serviceCode": getattr(Config, "VAKSMS_SERVICE", "gl"),
        "isBanned": False,
    }
    headers = await _vaksms_headers()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{VAKSMS_BASE}/number/cancel", json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                logger.info(f"vak-sms order {order_id} cancelled")
            else:
                text = await resp.text()
                logger.warning(f"vak-sms cancel failed ({resp.status}): {text[:160]}")


async def _finish_vaksms_order(order_id: str):
    # vak-sms has no explicit "finish" call; the number auto-closes when the
    # rental window ends. Reporting success is enough — the next /number/active
    # poll already reflects it. Kept as a no-op so the shared flow stays uniform.
    logger.info(f"vak-sms order {order_id} marked complete by verification")
