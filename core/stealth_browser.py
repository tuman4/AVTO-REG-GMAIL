import os
import random
import logging
import asyncio
from config.settings import Config
from core.behavior import HumanBehavior
from core.warmup import WarmupEngine
from core.pw import async_playwright
from core.proxy_manager import ProxyManager

JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "js")


def _load_js_file(filename):
    path = os.path.join(JS_DIR, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None

# Playwright stealth
try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None

logger = logging.getLogger('gmail_creator_stealth')

class PlaywrightStealthManager:
    """Manages the Playwright browser instance with advanced stealth capabilities"""
    
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_mobile = False

    async def initialize(self, proxy=None, is_premium=False):
        logger.info("Initializing Playwright Stealth Browser...")

        # Validate before starting the Playwright subprocess. This makes bad
        # proxy input fail deterministically even when the local browser
        # runtime itself cannot spawn a child process.
        proxy_settings = ProxyManager.format_for_playwright(proxy) if proxy else None
        if proxy and not proxy_settings:
            raise ValueError("Invalid proxy format; browser launch aborted")

        self.playwright = await async_playwright().start()
        
        launch_args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-features=IsolateOrigins,site-per-process,AutomationControlled',
            '--disable-infobars',
            '--disable-dev-shm-usage',
            '--disable-breakpad',
            '--disable-component-update',
            '--disable-domain-reliability',
            '--disable-sync',
            '--metrics-recording-only',
            '--no-first-run',
            '--no-default-browser-check',
            '--window-position=0,0',
        ]
        if Config.BROWSER_NO_SANDBOX:
            launch_args.extend(['--no-sandbox', '--disable-setuid-sandbox'])
        
        # A malformed proxy must fail closed. Silently continuing without it
        # leaks the host IP and makes the resulting session impossible to trust.
        if proxy_settings:
            parsed_proxy = ProxyManager.parse(proxy)
            logger.info(
                "Proxy configured: %s:%s%s",
                parsed_proxy["host"],
                parsed_proxy["port"],
                " (authenticated)" if parsed_proxy["user"] else "",
            )
                
        # Launch real chrome/chromium — try installed Chrome first, fall back to bundled Chromium
        try:
            self.browser = await self.playwright.chromium.launch(
                headless=Config.HEADLESS_MODE,
                args=launch_args,
                proxy=proxy_settings,
                channel="chrome"
            )
        except Exception:
            logger.info("Installed Chrome not found, falling back to bundled Chromium...")
            self.browser = await self.playwright.chromium.launch(
                headless=Config.HEADLESS_MODE,
                args=launch_args,
                proxy=proxy_settings,
            )
        

        # ── Fingerprint profile (randomized per session) ───────────────────────
        # Use the actual browser build. A random historical UA combined with
        # current Chromium Client Hints is internally inconsistent and causes
        # both flaky rendering and unnecessary security challenges.
        chrome_ver = self.browser.version

        screen_profiles = [
            {"width": 1920, "height": 1080, "aw": 1920, "ah": 1040},
            {"width": 1366, "height": 768,  "aw": 1366, "ah": 728},
            {"width": 1536, "height": 864,  "aw": 1536, "ah": 824},
            {"width": 1440, "height": 900,  "aw": 1440, "ah": 860},
            {"width": 2560, "height": 1440, "aw": 2560, "ah": 1400},
        ]
        sp  = random.choice(screen_profiles)
        hw  = random.choice([4, 6, 8, 12, 16])
        mem = random.choice([4, 8, 16])

        if is_premium:
            # Mobile emulation is explicit; keep its UA aligned with the
            # actual Chromium build instead of inventing an older version.
            ua = f"Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Mobile Safari/537.36"
            viewport = {"width": 412, "height": 915}
            is_mobile = True
            has_touch = True
        else:
            viewport = {"width": sp["width"], "height": sp["height"]}
            is_mobile = False
            has_touch = False

        context_options = {
            "viewport": viewport,
            "locale": Config.BROWSER_LOCALE,
            "has_touch": has_touch,
            "is_mobile": is_mobile,
            "color_scheme": "light",
        }
        if is_premium:
            context_options["user_agent"] = ua

        timezone = getattr(Config, "BROWSER_TIMEZONE", "")
        if timezone:
            context_options["timezone_id"] = timezone

        geolocation = getattr(Config, "BROWSER_GEOLOCATION", "")
        if geolocation:
            try:
                latitude, longitude = (float(value.strip()) for value in geolocation.split(",", 1))
                context_options["geolocation"] = {"latitude": latitude, "longitude": longitude}
                context_options["permissions"] = ["geolocation"]
            except (TypeError, ValueError):
                raise ValueError("BROWSER_GEOLOCATION must be `latitude,longitude`")

        self.context = await self.browser.new_context(**context_options)
        self.context.set_default_timeout(Config.BROWSER_TIMEOUT * 1000)
        self.context.set_default_navigation_timeout(Config.BROWSER_TIMEOUT * 1000)

        self.is_mobile = is_mobile
        self.page = await self.context.new_page()

        # ── Apply playwright-stealth ────────────────────────────────────────
        if stealth_async:
            try:
                await stealth_async(self.page)
                logger.info("Stealth plugin applied successfully.")
            except Exception as e:
                logger.warning(f"Failed to apply stealth plugin: {e}")

        # ── 12-point fingerprint spoofing (Clean & Dynamic) ────
        rtt     = random.choice([25, 50, 100, 150])
        dnl     = round(random.uniform(10, 100), 1)
        bat     = round(random.uniform(0.75, 1.0), 2)

        webgl_vendors = ["Google Inc. (NVIDIA)", "Google Inc. (Intel)", "Google Inc. (AMD)"]
        webgl_renderers = [
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ]
        gl_vendor = random.choice(webgl_vendors)
        gl_renderer = random.choice(webgl_renderers)

        if Config.ENABLE_FINGERPRINT_MASKING:
            await self.context.add_init_script(f"""
        (() => {{
            // 1. Remove webdriver flag
            try {{ delete navigator.__proto__.webdriver; }} catch(_) {{}}
            Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});

            // 2. Battery API
            if(navigator.getBattery){{navigator.getBattery=()=>Promise.resolve({{
                charging: true, chargingTime: 0, dischargingTime: Infinity, level: {bat},
                onchargingchange: null, onchargingtimechange: null, ondischargingtimechange: null, onlevelchange: null
            }});}}

            // 3. Network connection
            if (navigator.connection) {{
                Object.defineProperty(navigator, 'connection', {{ get: () =>
                    ({{'effectiveType':'4g','rtt':{rtt},'downlink':{dnl},'saveData':false,'type':'wifi','onchange':null}})
                }});
            }}

            // 4. Languages
            Object.defineProperty(navigator, 'languages', {{get: () => ['en-US', 'en']}});
            Object.defineProperty(navigator, 'language', {{get: () => 'en-US'}});

            // 5. Hardware concurrency & device memory
            Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hw}}});
            Object.defineProperty(navigator, 'deviceMemory', {{get: () => {mem}}});

            // 6. WebGL vendor/renderer spoof
            const origGetParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {{
                if (param === 37445) return '{gl_vendor}';
                if (param === 37446) return '{gl_renderer}';
                return origGetParameter.call(this, param);
            }};
            if (typeof WebGL2RenderingContext === 'undefined') return;
            const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {{
                if (param === 37445) return '{gl_vendor}';
                if (param === 37446) return '{gl_renderer}';
                return origGetParameter2.call(this, param);
            }};

            // 7. Plugins (Chrome always has at least these)
            Object.defineProperty(navigator, 'plugins', {{get: () => {{
                const p = [
                    {{name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
                    {{name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''}},
                    {{name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}}
                ];
                p.length = 3;
                p.namedItem = (n) => p.find(x => x.name === n) || null;
                p.item = (i) => p[i] || null;
                p.refresh = () => {{}};
                return p;
            }}}});

            // 8. Permissions API spoof (avoid "prompt" for notifications — real browsers have "denied" or "granted")
            const origQuery = Permissions.prototype.query;
            Permissions.prototype.query = function(desc) {{
                if (desc.name === 'notifications') return Promise.resolve({{state: 'denied', onchange: null}});
                return origQuery.call(this, desc);
            }};

            // 9. Screen properties
            Object.defineProperty(screen, 'availWidth', {{get: () => {sp['aw']}}});
            Object.defineProperty(screen, 'availHeight', {{get: () => {sp['ah']}}});
            Object.defineProperty(screen, 'colorDepth', {{get: () => 24}});
            Object.defineProperty(screen, 'pixelDepth', {{get: () => 24}});

            // 10. Remove Playwright/automation traces from all frames
            ['__playwright','__pw_manual','_playwrightBinding','__playwright__binding__',
             'cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
             'cdc_adoQpoasnfa76pfcZLmcfl_Symbol','__webdriver_evaluate','__selenium_evaluate',
             '__webdriver_script_function','__webdriver_script_func','__webdriver_script_fn',
             '__fxdriver_evaluate','__driver_evaluate','__webdriver_unwrapped',
             '__selenium_unwrapped','__fxdriver_unwrapped','_Selenium_IDE_Recorder',
             '_selenium','calledSelenium','_WEBDRIVER_ELEM_CACHE',
             'ChromeDriverw','driver-evaluate','webdriver-evaluate-response',
             'domAutomation','domAutomationController'
            ].forEach(k => {{ try {{ delete window[k]; }} catch(_) {{}} }});

            // 11. Chrome runtime (Google checks for this — must exist in Chrome)
            if (!window.chrome) {{
                window.chrome = {{
                    runtime: {{
                        connect: function() {{}},
                        sendMessage: function() {{}},
                        onMessage: {{addListener: function() {{}}, removeListener: function() {{}}}},
                        onConnect: {{addListener: function() {{}}, removeListener: function() {{}}}}
                    }},
                    loadTimes: function() {{ return {{}}; }},
                    csi: function() {{ return {{}}; }}
                }};
            }}

            // 12. Canvas fingerprint noise (subtle, not broken)
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {{
                if (this.width > 16 && this.height > 16) {{
                    try {{
                        const ctx = this.getContext('2d');
                        if (ctx) {{
                            const imgData = ctx.getImageData(0, 0, 1, 1);
                            imgData.data[0] = (imgData.data[0] + {random.randint(1, 3)}) % 256;
                            ctx.putImageData(imgData, 0, 0);
                        }}
                    }} catch(_) {{}}
                }}
                return origToDataURL.apply(this, arguments);
            }};
        }})();
            """)

        # Optional scripts must be registered before the first navigation so
        # warmup and signup observe the same page state.
        for enabled, filename, label in (
            (Config.ENABLE_POLTERGEIST, "poltergeist_fp.js", "Poltergeist FP"),
            (Config.ENABLE_GHOST_TYPER, "ghost_typer.js", "Ghost Typer"),
        ):
            if enabled:
                script = _load_js_file(filename)
                if script:
                    await self.context.add_init_script(script)
                    logger.info("%s script injected from js/%s", label, filename)

        # ── Session Warmup (builds trust cookies before signup) ─────────────
        # Premium runs need this too: a cold residential IP gets hit with the
        # device "Send SMS" challenge, which no rental number can answer. The
        # warmup is what makes the signup form's own phone field appear instead.
        if Config.ENABLE_SESSION_WARMING:
            logger.info("Session warming enabled — running warmup engine...")
            try:
                await WarmupEngine.run_warmup(self.page, duration_minutes=1)
            except Exception as wu_e:
                logger.warning(f"Warmup failed (non-fatal): {wu_e}")
        else:
            logger.info("Session warming disabled (skipping warmup).")

        logger.info(
            "Browser: Chromium/%s | %sx%s | locale=%s | masking=%s",
            chrome_ver,
            sp["width"],
            sp["height"],
            Config.BROWSER_LOCALE,
            Config.ENABLE_FINGERPRINT_MASKING,
        )
        return True


    async def close(self):
        try:
            if self.page:
                await self.page.close()
        except Exception:
            pass
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None
            
    async def natural_type(self, selector, text):
        try:
            return await HumanBehavior.natural_type(self.page, selector, text, make_typos=Config.ENABLE_HUMAN_TYPING_ERRORS)
        except Exception as e:
            logger.warning(f"natural_type failed for '{selector}': {e}")
            return False
        
    async def natural_click(self, selector):
        try:
            element = await self.page.wait_for_selector(selector, timeout=8000)
            if element:
                box = await element.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2 + random.uniform(-2, 2)
                    y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
                    await self.page.mouse.move(x, y, steps=10)
                    await self.page.mouse.click(x, y)
                else:
                    await element.click()
                return True
            return False
        except Exception as e:
            logger.warning(f"natural_click failed for '{selector}': {e}")
            return False

    async def _wait_for_birthday_page(self, timeout_ms=20000):
        """Wait until the birthday/gender page is visible with multiple selector attempts."""
        birthday_selectors = [
            'input[name="day"]',
            'select#month',
            'select[name="month"]',
            'input[name="year"]',
            '#day', '#month', '#year',
        ]
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for sel in birthday_selectors:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.3)
        return False

    async def fill_birthday_gender(self, month, day, year, gender):
        """Fill birthday (month, day, year) and gender on Google signup page."""
        try:
            logger.info(f"fill_birthday_gender: month={month}, day={day}, year={year}, gender={gender}")

            page_found = await self._wait_for_birthday_page(timeout_ms=20000)
            if not page_found:
                logger.warning("Birthday page not detected within 20s, attempting to fill anyway.")

            await self.page.wait_for_timeout(500)

            month_names = ["January", "February", "March", "April", "May", "June",
                           "July", "August", "September", "October", "November", "December"]
            try:
                month_idx = int(month)
                if not (1 <= month_idx <= 12):
                    month_idx = 1
            except Exception:
                month_idx = 1
            month_text = month_names[month_idx - 1]
            month_str = str(month_idx)

            # ─── MONTH ───────────────────────────────────────────────────────
            month_ok = False

            for sel in ['select#month', 'select[name="month"]', 'select[aria-label*="onth" i]',
                        'select[aria-label*="شهر"]']:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        try:
                            await el.select_option(value=month_str)
                            month_ok = True
                            break
                        except Exception:
                            pass
                        try:
                            await el.select_option(label=month_text)
                            month_ok = True
                            break
                        except Exception:
                            pass
                except Exception:
                    continue

            if not month_ok:
                for trigger_sel in ['#month', '[aria-label*="Month"]', '[placeholder="Month"]',
                                    'div[data-id="month"]', 'span:has-text("Month")']:
                    try:
                        trigger = await self.page.query_selector(trigger_sel)
                        if trigger and await trigger.is_visible():
                            await trigger.click()
                            await self.page.wait_for_timeout(500)
                            for opt_sel in [
                                f'li[data-value="{month_str}"]',
                                f'[role="option"][data-value="{month_str}"]',
                                f'[role="option"]:has-text("{month_text}")',
                                f'li:has-text("{month_text}")',
                            ]:
                                try:
                                    opt = await self.page.wait_for_selector(opt_sel, timeout=1500)
                                    if opt:
                                        await opt.click()
                                        month_ok = True
                                        break
                                except Exception:
                                    continue
                            if month_ok:
                                break
                    except Exception:
                        continue

            if not month_ok:
                await self.page.evaluate("""(m) => {
                    const s = document.querySelector('select#month')
                           || document.querySelector('select[name="month"]');
                    if (s) {
                        s.value = m;
                        s.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", month_str)

            await self.page.wait_for_timeout(300)

            # ─── DAY ─────────────────────────────────────────────────────────
            day_ok = False
            for sel in ['input[name="day"]', '#day', 'input[aria-label*="Day" i]',
                        'input[placeholder="Day"]']:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await self.page.wait_for_timeout(100)
                        await el.click(click_count=3)
                        await el.fill(day)
                        await el.dispatch_event("input")
                        day_ok = True
                        break
                except Exception:
                    continue

            if not day_ok:
                await self.page.evaluate("""(d) => {
                    const inp = document.querySelector('input[name="day"]');
                    if (inp) { inp.value = d; inp.dispatchEvent(new Event('input', { bubbles: true })); }
                }""", day)

            await self.page.wait_for_timeout(300)

            # ─── YEAR ────────────────────────────────────────────────────────
            year_ok = False
            for sel in ['input[name="year"]', '#year', 'input[aria-label*="Year" i]',
                        'input[placeholder="Year"]']:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        attr_name = await el.get_attribute("name")
                        if attr_name and attr_name.lower() == "day":
                            continue
                        await el.click()
                        await self.page.wait_for_timeout(100)
                        await el.click(click_count=3)
                        await el.fill(year)
                        await el.dispatch_event("input")
                        year_ok = True
                        break
                except Exception:
                    continue

            if not year_ok:
                await self.page.evaluate("""(y) => {
                    const inp = document.querySelector('input[name="year"]');
                    if (inp) { inp.value = y; inp.dispatchEvent(new Event('input', { bubbles: true })); }
                }""", year)

            await self.page.wait_for_timeout(300)

            # ─── GENDER (fast & correct) ─────────────────────────────────────
            # Debug: Log what select elements exist on the page
            try:
                select_info = await self.page.evaluate("""() => {
                    const selects = document.querySelectorAll('select');
                    const info = [];
                    for (const s of selects) {
                        const opts = Array.from(s.options).map(o => o.value + '=' + o.textContent.trim());
                        info.push({id: s.id, name: s.name, visible: s.offsetParent !== null, opts: opts});
                    }
                    return JSON.stringify(info);
                }""")
                logger.info(f"Page select elements: {select_info}")
            except Exception:
                pass

            gender_ok = False
            gender_text_map = {"1": "Male", "2": "Female", "3": "Rather not say", "0": "Rather not say"}
            gtext = gender_text_map.get(str(gender), "Male")

            # Google's <select> value mapping: 1=Female, 2=Male, 3=Rather not say, 4=Custom
            google_gender_val = {"1": "2", "2": "1", "3": "3", "0": "3"}
            gval = google_gender_val.get(str(gender), "2")

            # Strategy A: Super aggressive JS — find ANY select on the page and set it
            # (birthday uses <select> for month too, so we skip selects with month-like options)
            try:
                result = await self.page.evaluate("""(args) => {
                    const [gval, gtext] = args;
                    // Collect all selects on the page
                    const allSelects = document.querySelectorAll('select');
                    let genderSelect = null;

                    for (const sel of allSelects) {
                        // Skip month selects (they have January/February etc)
                        let isMonth = false;
                        for (const opt of sel.options) {
                            const t = opt.textContent.trim().toLowerCase();
                            if (t === 'january' || t === 'february' || t === 'march' ||
                                t === 'يناير' || t === 'فبراير') {
                                isMonth = true; break;
                            }
                        }
                        if (isMonth) continue;

                        // Check if this has gender-like options (Male/Female/Rather not say)
                        let hasGenderOption = false;
                        for (const opt of sel.options) {
                            const t = opt.textContent.trim().toLowerCase();
                            if (t === 'male' || t === 'female' || t === 'rather not say' ||
                                t === 'ذكر' || t === 'أنثى' || t.includes('gender') ||
                                t.includes('الجنس')) {
                                hasGenderOption = true; break;
                            }
                        }
                        if (hasGenderOption || sel.id === 'gender' || sel.name === 'gender') {
                            genderSelect = sel; break;
                        }

                        // If only one non-month select remains, it's probably gender
                        if (!genderSelect && allSelects.length <= 2) {
                            genderSelect = sel;
                        }
                    }

                    if (!genderSelect) return 'no_gender_select_found';

                    // Log what we found for debugging
                    const optionTexts = Array.from(genderSelect.options).map(o => o.value + ':' + o.textContent.trim());
                    const info = 'id=' + genderSelect.id + ',name=' + genderSelect.name + ',opts=' + optionTexts.join('|');

                    // Method 1: Match by option text
                    for (const opt of genderSelect.options) {
                        const txt = opt.textContent.trim().toLowerCase();
                        if (txt === gtext.toLowerCase()) {
                            genderSelect.selectedIndex = opt.index;
                            genderSelect.value = opt.value;
                            genderSelect.dispatchEvent(new Event('input', { bubbles: true }));
                            genderSelect.dispatchEvent(new Event('change', { bubbles: true }));
                            genderSelect.dispatchEvent(new Event('blur', { bubbles: true }));
                            return 'ok_text:' + opt.value + '|' + info;
                        }
                    }

                    // Method 2: Set by value directly
                    genderSelect.value = gval;
                    genderSelect.selectedIndex = parseInt(gval);
                    genderSelect.dispatchEvent(new Event('input', { bubbles: true }));
                    genderSelect.dispatchEvent(new Event('change', { bubbles: true }));
                    genderSelect.dispatchEvent(new Event('blur', { bubbles: true }));
                    return 'ok_val:' + gval + '|' + info;
                }""", [gval, gtext])
                if result and result.startswith('ok'):
                    gender_ok = True
                    logger.info(f"Gender set via JS: {result}")
                else:
                    logger.warning(f"Gender JS result: {result}")
            except Exception as e:
                logger.debug(f"Gender JS strategy failed: {e}")

            # Strategy B: Playwright select_option on all matching selectors
            if not gender_ok:
                gender_selectors = ['select#gender', 'select[name="gender"]', 'select[id="gender"]',
                                    'select[aria-label*="ender" i]', 'select[aria-label*="جنس"]',
                                    'select']
                for sel in gender_selectors:
                    try:
                        elements = await self.page.query_selector_all(sel)
                        for el in elements:
                            if not await el.is_visible():
                                continue
                            # Skip month select
                            try:
                                tag_name = await el.evaluate("e => e.id || e.name || ''")
                                if 'month' in str(tag_name).lower():
                                    continue
                            except Exception:
                                pass
                            try:
                                await el.select_option(value=gval)
                                gender_ok = True
                                logger.info(f"Gender set via select_option(value={gval})")
                                break
                            except Exception:
                                pass
                            try:
                                await el.select_option(label=gtext)
                                gender_ok = True
                                logger.info(f"Gender set via select_option(label={gtext})")
                                break
                            except Exception:
                                pass
                        if gender_ok:
                            break
                    except Exception:
                        continue

            # Strategy C: Focus the select + keyboard to select option
            if not gender_ok:
                try:
                    # Find gender select via evaluate and focus it
                    focused = await self.page.evaluate("""() => {
                        const allSelects = document.querySelectorAll('select');
                        for (const sel of allSelects) {
                            if (sel.id === 'gender' || sel.name === 'gender') {
                                sel.focus(); return true;
                            }
                            // Check if NOT a month select
                            let isMonth = false;
                            for (const opt of sel.options) {
                                const t = opt.textContent.trim().toLowerCase();
                                if (['january','february','march','april','may','june'].includes(t)) {
                                    isMonth = true; break;
                                }
                            }
                            if (!isMonth && sel.options.length >= 3) {
                                sel.focus(); return true;
                            }
                        }
                        return false;
                    }""")
                    if focused:
                        await self.page.wait_for_timeout(300)
                        # Arrow down to select the right option
                        presses = int(gval)
                        for _ in range(presses):
                            await self.page.keyboard.press("ArrowDown")
                            await self.page.wait_for_timeout(100)
                        await self.page.wait_for_timeout(200)
                        gender_ok = True
                        logger.info(f"Gender set via keyboard ({presses} ArrowDown)")
                except Exception as e:
                    logger.debug(f"Gender keyboard strategy failed: {e}")

            # Strategy D: Handle Material Design / Web Component dropdowns
            # Google may use <md-outlined-select>, div[role="listbox"], or custom elements
            if not gender_ok:
                try:
                    result = await self.page.evaluate("""(gtext) => {
                        // Check for Material Design select components
                        const mdSelects = document.querySelectorAll(
                            'md-outlined-select, md-filled-select, [role="listbox"], [role="combobox"], ' +
                            'div[data-value], div.VfPpkd-TkwUic, div[jsname]'
                        );

                        // Also check for any clickable dropdown-like elements with Gender text
                        const allElements = document.querySelectorAll('*');
                        let genderContainer = null;
                        for (const el of allElements) {
                            const text = el.textContent.trim();
                            if ((text === 'Gender' || text === 'الجنس') &&
                                el.tagName !== 'LABEL' && el.tagName !== 'SPAN' &&
                                el.offsetParent !== null) {
                                genderContainer = el.closest('[role="listbox"], [role="combobox"], [jscontroller]') || el;
                                break;
                            }
                        }

                        if (genderContainer) {
                            genderContainer.click();
                            return 'clicked_container';
                        }
                        return 'no_md_component';
                    }""", gtext)

                    if result == 'clicked_container':
                        await self.page.wait_for_timeout(800)
                        # Now find and click the option text (Male/Female etc)
                        try:
                            option_clicked = await self.page.evaluate("""(gtext) => {
                                const items = document.querySelectorAll(
                                    '[role="option"], [role="menuitem"], li, md-select-option, ' +
                                    'div[data-value], span'
                                );
                                for (const item of items) {
                                    const t = item.textContent.trim().toLowerCase();
                                    if (t === gtext.toLowerCase() && item.offsetParent !== null) {
                                        item.click();
                                        return 'clicked:' + t;
                                    }
                                }
                                return 'no_option';
                            }""", gtext)
                            if option_clicked and option_clicked.startswith('clicked'):
                                gender_ok = True
                                logger.info(f"Gender set via MD component: {option_clicked}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Gender MD component strategy failed: {e}")

            if not gender_ok:
                logger.warning(f"Could not select gender '{gtext}' — continuing anyway")

            await self.page.wait_for_timeout(400)
            return True

        except Exception as e:
            logger.error(f"fill_birthday_gender error: {e}", exc_info=True)
            return True
