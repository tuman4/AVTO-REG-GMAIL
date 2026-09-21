"""
Selenium Runner - Chrome WebDriver-based Gmail account creation flow
Handles driver creation, account creation, and verification for Selenium engine.
"""
import os
import glob
import json
import time
import random
import secrets
import logging
import tempfile
import shutil
import uuid
import string

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None

from config.settings import Config
from core.fingerprint import inject_selenium_poltergeist
from core.trust_builder import (
    warm_up_session, ghost_mode_prepare,
)
from core.account_manager import account_manager

logger = logging.getLogger('gmail_creator_selenium')

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

SCREEN_SIZES = [
    (1366, 768), (1440, 900), (1536, 864),
    (1600, 900), (1920, 1080), (1280, 720),
]


def _load_names():
    names_file = Config.NAMES_FILE if hasattr(Config, 'NAMES_FILE') else "data/names.txt"
    names = []
    try:
        with open(names_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
    except FileNotFoundError:
        pass
    return names


_names_list = _load_names()


def generate_name():
    if _names_list:
        return random.choice(_names_list)
    return f"User{random.randint(1000, 9999)}"


def generate_password(length=14):
    """Generate a strong unique password per account using the secrets module."""
    length = max(length, 14)
    upper = [secrets.choice(string.ascii_uppercase) for _ in range(3)]
    lower = [secrets.choice(string.ascii_lowercase) for _ in range(5)]
    digits = [secrets.choice(string.digits) for _ in range(3)]
    specials = [secrets.choice("!@#$%&*") for _ in range(2)]
    filler = [secrets.choice(string.ascii_letters + string.digits)
              for _ in range(length - 13)]
    pool = upper + lower + digits + specials + filler
    secrets.SystemRandom().shuffle(pool)
    return "".join(pool)


def validate_birthday(birthday_str):
    """Accept 'M D Y', 'MM/DD/YYYY', 'YYYY-MM-DD', 'February 4, 1990' — never silently
    fall back to January 1st."""
    if not birthday_str:
        return "2", "4", "1990"
    s = str(birthday_str).strip()
    month_names = {n.lower(): i + 1 for i, n in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"])}

    parsed = None
    for sep in ("/", "-"):
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if len(parts) == 3:
                parsed = parts
            break
    if parsed is None:
        parts = s.split()
        if len(parts) == 3:
            parsed = parts
        elif len(parts) >= 3 and parts[0].lower() in month_names:
            parsed = [parts[0], parts[1].rstrip(","), parts[-1]]

    if parsed is None:
        logger.warning(f"Unparseable birthday {birthday_str!r}, using default 2 4 1990")
        return "2", "4", "1990"

    try:
        m_raw, d_raw, y_raw = parsed
        if m_raw.lower() in month_names:
            month = month_names[m_raw.lower()]
        else:
            month = int(m_raw)
        day = int(d_raw)
        year = int(y_raw)
    except (ValueError, TypeError):
        logger.warning(f"Unparseable birthday {birthday_str!r}, using default 2 4 1990")
        return "2", "4", "1990"

    if not (1 <= month <= 12):
        month = 2
    if not (1 <= day <= 31):
        day = 4
    if not (1900 <= year <= 2010):
        year = 1990
    return str(month), str(day), str(year)


def _parse_proxy(proxy_string):
    """Parse proxy string into components: host:port or user:pass@host:port."""
    try:
        proxy_string = proxy_string.strip()
        if not proxy_string:
            return None

        if "@" in proxy_string:
            auth, hostport = proxy_string.rsplit("@", 1)
            user, passwd = auth.split(":", 1)
            host, port = hostport.rsplit(":", 1)
            return {"host": host, "port": port, "user": user, "pass": passwd}
        else:
            parts = proxy_string.rsplit(":", 1)
            if len(parts) == 2:
                return {"host": parts[0], "port": parts[1], "user": None, "pass": None}
    except Exception:
        pass
    return None


def _install_proxy_auth_extension(chrome_options, parsed):
    """Inject proxy credentials via a packed extension (Selenium can't pass auth
    through --proxy-server, and dropping it leaks the real IP)."""
    try:
        import zipfile

        ext_dir = tempfile.mkdtemp(prefix="proxyauth_")
        manifest = json.dumps({
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Proxy Auth",
            "permissions": ["proxy", "tabs", "unlimitedStorage",
                            "storage", "<all_urls>", "webRequest",
                            "webRequestBlocking"],
            "background": {"scripts": ["background.js"]},
        }, indent=2)
        background = (
            "var config = {\n"
            f"  mode: 'fixed_servers',\n"
            "  rules: {\n"
            "    singleProxy: {\n"
            f"      scheme: 'http',\n"
            f"      host: '{parsed['host']}',\n"
            f"      port: {int(parsed['port'])}\n"
            "    },\n"
            "    bypassList: ['localhost']\n"
            "  }\n"
            "};\n"
            "chrome.proxy.settings.set({value: config, scope: 'regular'},\n"
            "  function() {});\n"
            "function callbackFn(details) {\n"
            "  return {\n"
            f"    authCredentials: {{username: '{parsed['user']}',\n"
            f"      password: '{parsed['pass'] or ''}'}}\n"
            "  };\n"
            "}\n"
            "chrome.webRequest.onAuthRequired.addListener(\n"
            "  callbackFn, {urls: ['<all_urls>']}, ['blocking']);\n"
        )

        with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(manifest)
        with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
            f.write(background)

        ext_path = os.path.join(ext_dir, "proxy_auth.zip")
        with zipfile.ZipFile(ext_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(os.path.join(ext_dir, "manifest.json"), "manifest.json")
            z.write(os.path.join(ext_dir, "background.js"), "background.js")

        chrome_options.add_argument(f"--load-extension={ext_path}")
    except Exception as e:
        logger.error(f"Failed to build proxy-auth extension: {e}")


def create_driver(proxy=None):
    """Create and configure a Chrome driver with a unique throwaway profile.

    Returns ``(driver, profile_dir)`` — the caller owns both and must quit the
    driver and remove the profile directory. Returns ``(None, None)`` on failure.
    """
    try:
        chrome_options = ChromeOptions()

        profile_id = str(uuid.uuid4())[:8]
        profile_dir = os.path.join(tempfile.gettempdir(), f"chrome_profile_{profile_id}")
        os.makedirs(profile_dir, exist_ok=True)
        chrome_options.add_argument(f'--user-data-dir={profile_dir}')

        width, height = random.choice(SCREEN_SIZES)
        chrome_options.add_argument(f'--window-size={width},{height}')
        # Chrome only treats --/-prefixed args as switches; a bare user-agent=...
        # was parsed as a URL to open, so UA rotation never applied AND a junk
        # navigation fired at startup.
        chrome_options.add_argument(f'--user-agent={random.choice(USER_AGENTS)}')

        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument("--disable-webrtc")
        chrome_options.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-software-rasterizer')
        chrome_options.add_argument('--disable-logging')
        chrome_options.add_argument('--log-level=3')
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-ssl-errors')
        chrome_options.add_argument('--no-experiments')
        chrome_options.add_argument('--no-default-browser-check')
        chrome_options.add_argument('--no-first-run')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-popup-blocking')

        if Config.HEADLESS_MODE:
            chrome_options.add_argument('--headless=new')
            # Keep the randomized window size; the old override flattened every
            # headless session to 1920x1080.
            chrome_options.add_argument(f'--window-size={width},{height}')

        if proxy:
            parsed = _parse_proxy(proxy)
            if not parsed:
                logger.warning(f"Proxy {proxy!r} could not be parsed — running without a proxy")
            elif parsed["user"]:
                # --proxy-server cannot carry credentials, so authenticated proxies
                # need the classic manifest-based extension or they are silently
                # dropped and traffic exits from the real IP.
                _install_proxy_auth_extension(chrome_options, parsed)
                logger.info(f"Using authenticated proxy: {parsed['host']}:{parsed['port']}")
            else:
                chrome_options.add_argument(f'--proxy-server={parsed["host"]}:{parsed["port"]}')
                logger.info(f"Using proxy: {parsed['host']}:{parsed['port']}")

        service = _get_chrome_service()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                driver = webdriver.Chrome(service=service, options=chrome_options)

                # Session-stable fingerprint values. The old snippet re-randomized
                # hardwareConcurrency/deviceMemory on every read and deviceMemory
                # could return 16 (impossible — the spec caps at 8), which is a
                # stronger bot signal than no spoof at all.
                hw_concurrency = random.choice([4, 6, 8, 12])
                device_memory = random.choice([2, 4, 8])
                driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                    'source': f'''
                        Object.defineProperty(navigator, 'webdriver', {{get: () => false}});
                        Object.defineProperty(navigator, 'hardwareConcurrency', {{
                            get: () => {hw_concurrency}
                        }});
                        Object.defineProperty(navigator, 'deviceMemory', {{
                            get: () => {device_memory}
                        }});
                        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                        HTMLCanvasElement.prototype.toDataURL = function(type) {{
                            try {{
                                if (this.width > 0 && this.height > 0) {{
                                    const ctx = this.getContext('2d');
                                    if (ctx) {{
                                        ctx.save();
                                        ctx.fillStyle = 'rgba(0,0,0,0.01)';
                                        ctx.fillRect(0, 0, 1, 1);
                                        ctx.restore();
                                    }}
                                }}
                            }} catch (e) {{}}
                            return originalToDataURL.apply(this, arguments);
                        }};
                    '''
                })

                driver.set_page_load_timeout(30)
                driver.get("https://www.google.com")
                time.sleep(2)
                logger.info("Selenium browser created successfully")
                return driver, profile_dir
            except Exception as e:
                logger.warning(f"Browser creation attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                try:
                    if 'driver' in locals():
                        driver.quit()
                except Exception:
                    pass
                time.sleep(2)

    except Exception as e:
        logger.error(f"Failed to create Selenium driver: {e}")
        return None, None


def _get_chrome_service():
    """Try multiple methods to get a ChromeDriver service."""
    if ChromeDriverManager:
        try:
            path = ChromeDriverManager().install()
            if path and os.path.exists(path) and os.path.isfile(path):
                return ChromeService(path)
        except Exception:
            pass

    common_paths = [
        os.path.join(os.getcwd(), "chromedriver.exe"),
        "C:\\chromedriver\\chromedriver.exe",
    ]
    wdm_glob = os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver", "*", "chromedriver.exe")
    matches = glob.glob(wdm_glob)
    if matches:
        common_paths.insert(0, matches[0])

    for path in common_paths:
        if os.path.exists(path) and os.path.isfile(path):
            return ChromeService(path)

    return ChromeService()


def _visible_page_text(driver):
    """Visible text only — script/style/base64 content excluded so substring
    detection cannot match a data URI or a minified variable name."""
    try:
        return driver.execute_script("""
            var walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT, null, false);
            var chunks = [];
            var node;
            while ((node = walker.nextNode())) {
                var p = node.parentElement;
                if (!p) continue;
                var tag = p.tagName.toLowerCase();
                if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
                var t = node.textContent.trim();
                if (t) chunks.push(t);
            }
            return chunks.join(' ');
        """) or ""
    except Exception:
        return ""


def _click_next(driver):
    """Find and click the Next button using multiple strategies."""
    selectors = [
        "//button[contains(text(), 'Next')]",
        "//button[contains(@class, 'VfPpkd-LgbsSe')]",
        "//button[@type='submit']",
        "//button[contains(@aria-label, 'Next')]",
        "//span[contains(text(), 'Next')]/parent::button",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    time.sleep(0.5)
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue
    return False


def _human_typing(element, text, delay_range=(0.08, 0.18)):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(*delay_range))


def _fill_field(driver, element, value):
    """Fill a field using multiple methods."""
    try:
        element.clear()
        time.sleep(0.3)
        element.send_keys(value)
        return True
    except Exception:
        pass
    try:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            element, value
        )
        return True
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).click().send_keys(value).perform()
        return True
    except Exception:
        return False


def create_account_selenium(driver, wait, username, password, birthday_str, gender,
                            mode="standard", progress=None, task_id=None):
    """
    Create a Gmail account using Selenium WebDriver.

    Returns: (success: bool, error_type: str or None)
    """
    try:
        if mode == "youtube":
            driver.get("https://accounts.google.com/signup/v2/webcreateaccount?"
                       "continue=https://www.youtube.com/&flowName=GlifWebSignIn&flowEntry=SignUp&hl=en")
        elif mode == "workspace":
            driver.get("https://accounts.google.com/signup/v2/webcreateaccount?"
                       "continue=https://workspace.google.com/&flowName=GlifWebSignIn&flowEntry=SignUp&hl=en")
        else:
            # hl=en pins the UI language so English text selectors match regardless
            # of the proxy's geo (Google localizes by connection otherwise).
            driver.get("https://accounts.google.com/signup/v2/createaccount?"
                       "flowName=GlifWebSignIn&flowEntry=SignUp&hl=en")

        wait.until(EC.presence_of_element_located((By.NAME, "firstName")))
        time.sleep(random.uniform(1, 3))

        full_name = generate_name()
        parts = full_name.split()
        first_name = parts[0] if parts else "User"
        last_name = parts[-1] if len(parts) > 1 else "User"

        first_el = driver.find_element(By.NAME, "firstName")
        _fill_field(driver, first_el, first_name)

        last_el = driver.find_element(By.NAME, "lastName")
        _fill_field(driver, last_el, last_name)

        time.sleep(1.5)
        _click_next(driver)
        time.sleep(3)

        month, day, year = validate_birthday(birthday_str)

        driver.execute_script("""
            var sel = document.getElementById('month');
            if (sel) { sel.value = arguments[0]; sel.dispatchEvent(new Event('change', {bubbles:true})); }
        """, month)
        time.sleep(0.5)

        driver.execute_script("""
            var d = document.querySelector('input[name="day"]');
            if (d) { d.value = arguments[0]; d.dispatchEvent(new Event('input', {bubbles:true})); }
            var y = document.querySelector('input[name="year"]');
            if (y) { y.value = arguments[1]; y.dispatchEvent(new Event('input', {bubbles:true})); }
        """, day, year)

        driver.execute_script("""
            var sel = document.getElementById('gender');
            if (sel) { sel.value = arguments[0]; sel.dispatchEvent(new Event('change', {bubbles:true})); }
        """, gender)
        time.sleep(1)

        _click_next(driver)
        time.sleep(3)

        # Find and click "Create your own Gmail address"
        _select_create_own(driver)
        time.sleep(2)

        # Fill username
        username_field = _find_username_field(driver, wait)
        if username_field:
            _fill_field(driver, username_field, username)
            time.sleep(1)
            _click_next(driver)
            time.sleep(2)
        else:
            logger.error("Username field not found")
            return False, "USERNAME_FIELD_NOT_FOUND"

        # Fill password
        try:
            pw_field = wait.until(EC.presence_of_element_located((By.NAME, "Passwd")))
            confirm_field = wait.until(EC.presence_of_element_located((By.NAME, "PasswdAgain")))
            wait.until(EC.element_to_be_clickable((By.NAME, "Passwd")))

            pw_field.clear()
            confirm_field.clear()
            time.sleep(0.5)
            _human_typing(pw_field, password, (0.05, 0.12))
            time.sleep(0.5)
            _human_typing(confirm_field, password, (0.05, 0.12))
            time.sleep(1)

            _click_next(driver)
            time.sleep(3)
        except Exception as e:
            logger.error(f"Password entry failed: {e}")
            return False, "PASSWORD_ENTRY_FAILED"

        # Check what page we're on now.
        # Must scope to visible text: page_source includes base64 assets and inline
        # scripts, so a bare "qr" substring matched on essentially every page and
        # turned successes into QR_BLOCKED false positives.
        visible_text = _visible_page_text(driver).lower()
        current_url = (driver.current_url or "").lower()

        if "devicephonever" in current_url or "phonechallenge" in current_url:
            logger.warning("Phone verification detected (URL)")
            return False, "PHONE_REQUIRED"

        phone_signals = ["verify your phone", "add a phone number", "enter a phone number",
                         "get a verification code", "phone number", "verify it's you"]
        if any(s in visible_text for s in phone_signals):
            logger.warning("Phone verification detected")
            return False, "PHONE_REQUIRED"

        qr_signals = ["scan the qr", "qr code", "verify some info",
                      "use your android phone", "use an android phone"]
        if any(s in visible_text for s in qr_signals):
            logger.warning("QR code verification detected")
            return False, "QR_BLOCKED"

        email = f"{username}@gmail.com"
        account_manager.save(
            email=email, password=password,
            first_name=first_name, last_name=last_name,
            strategy=f"selenium_{mode}",
        )
        logger.info(f"Account created: {email}")
        return True, None

    except Exception as e:
        logger.error(f"Selenium account creation error: {e}")
        return False, "UNKNOWN_ERROR"


def _select_create_own(driver):
    """Try to click 'Create your own Gmail address' option."""
    selectors = [
        "//div[contains(text(), 'Create your own Gmail address')]",
        "//span[contains(text(), 'Create your own Gmail address')]",
        "//span[contains(text(), 'Create your own')]",
        "//div[contains(text(), 'Create your own')]",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    time.sleep(0.5)
                    el.click()
                    return True
        except Exception:
            continue
    return False


def _find_username_field(driver, wait):
    """Find the username input field using multiple strategies."""
    selectors = [
        "//input[@type='text' and contains(@name, 'user')]",
        "//input[@type='text' and contains(@id, 'user')]",
        "//input[@type='text' and contains(@aria-label, 'email')]",
        "//input[@type='text' and contains(@aria-label, 'Email')]",
        "//input[@name='Username']",
        "//input[@jsname='YPqjbf']",
        "//input[contains(@class, 'whsOnd')]",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            continue

    try:
        return wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='text']")))
    except Exception:
        return None


def run_selenium_flow(i, num_accounts, username, password, warmup_minutes=5,
                      stealth_mode=True, mode="standard", proxy=None):
    """
    Complete Selenium-based account creation flow.

    Returns: bool (success)
    """
    driver, profile_dir = create_driver(proxy=proxy)
    if not driver:
        return False

    try:
        wait = WebDriverWait(driver, Config.BROWSER_TIMEOUT)

        if stealth_mode:
            ghost_mode_prepare(driver, warmup_minutes)
        else:
            if Config.ENABLE_FINGERPRINT_MASKING:
                inject_selenium_poltergeist(driver)
            if Config.ENABLE_SESSION_WARMING:
                warm_up_session(driver)

        birthday = Config.YOUR_BIRTHDAY
        gender = str(Config.YOUR_GENDER)

        success, error_type = create_account_selenium(
            driver, wait, username, password,
            birthday, gender, mode=mode,
        )

        # Post-creation account warming
        if success and Config.ENABLE_SESSION_WARMING:
            try:
                from core.account_warmer import warm_account_selenium
                warm_account_selenium(f"{username}@gmail.com", password, duration_minutes=2)
            except Exception:
                pass

        # Telegram notification
        if success:
            try:
                from core.telegram_notifier import notifier
                notifier.notify_account_created(
                    email=f"{username}@gmail.com", password=password,
                    strategy=mode, proxy=proxy or "",
                )
            except Exception:
                pass

        return success

    except Exception as e:
        logger.error(f"Selenium flow error: {e}")
        return False
    finally:
        # Clean up the temp profile directory too, not just the driver — v1 left
        # one profile dir per account accumulating on disk forever.
        try:
            driver.quit()
        except Exception:
            pass
        if profile_dir and os.path.isdir(profile_dir):
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass
