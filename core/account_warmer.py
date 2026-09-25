"""
Post-Creation Account Warmer - Login and warm up newly created accounts
Makes accounts appear more legitimate by simulating real user activity.
"""
import asyncio
import time
import random
import logging

logger = logging.getLogger('gmail_creator_postwarmer')

_WARM_SEARCHES = [
    "weather this week", "best coffee near me", "how to backup gmail",
    "google account security check", "free online courses",
    "what is my ip", "how to recover email password", "news today",
]

_WARM_SERVICES = [
    "https://news.google.com/",
    "https://maps.google.com/",
    "https://drive.google.com/",
    "https://translate.google.com/",
    "https://photos.google.com/",
    "https://myaccount.google.com/",
]


async def warm_existing_session(page, duration_minutes=5):
    """
    Warm the account in the SAME browser context it was registered in.

    After registration the page is already logged in, already has the signup
    session's cookies, fingerprint and proxy. Opening a second browser to
    "warm" the account would re-login from a different IP and a different
    fingerprint — which looks to Google like account theft and triggers the
    very phone challenge the warmup is meant to prevent.

    This function only works with a page that is already authenticated.
    """
    if not page:
        return False

    logger.info(f"Warming existing session for {duration_minutes} minutes...")
    end = time.time() + duration_minutes * 60

    # Same guard as WarmupEngine: this warmer plays a YouTube video for 25-60s,
    # which alone streams more bytes than the entire signup. Trust comes from
    # the logged-in page views, not from buffering video.
    heavy_types = ("image/", "video/", "audio/", "font/")
    heavy_hosts = (
        "googlevideo.com", "ytimg.com", "ggpht.com", "googleusercontent.com",
    )

    async def _block_heavy(route):
        req = route.request
        restype = req.resource_type or ""
        url = req.url or ""
        if restype in ("image", "media", "font", "preload"):
            await route.abort()
            return
        if restype in ("xhr", "fetch") and any(h in url for h in heavy_hosts):
            await route.abort()
            return
        content_type = (req.headers or {}).get("content-type", "").lower()
        if any(t in content_type for t in heavy_types):
            await route.abort()
            return
        if any(h in url for h in heavy_hosts):
            await route.abort()
            return
        await route.continue_()

    try:
        await page.route("**/*", _block_heavy)
    except Exception as block_err:
        logger.debug(f"warmer: heavy-asset blocking unavailable: {block_err}")

    async def _dwell(lo, hi):
        await page.wait_for_timeout(random.randint(lo * 1000, hi * 1000))

    async def _scroll():
        try:
            dist = random.randint(200, 700)
            steps = random.randint(3, 6)
            for _ in range(steps):
                await page.mouse.wheel(0, dist // steps)
                await page.wait_for_timeout(random.randint(120, 320))
        except Exception:
            pass

    async def _youtube():
        # Watch time is the single strongest Google-ecosystem trust signal.
        try:
            await page.goto("https://www.youtube.com", timeout=20000,
                            wait_until="domcontentloaded")
            await _dwell(2, 4)
            await _scroll()
            thumbs = await page.query_selector_all("a#thumbnail")
            if thumbs and len(thumbs) > 2:
                await random.choice(thumbs[:8]).click(timeout=4000)
                await _dwell(25, 60)
                await _scroll()
        except Exception as e:
            logger.debug(f"warmer/youtube: {e}")

    async def _search():
        try:
            await page.goto("https://www.google.com", timeout=20000,
                            wait_until="domcontentloaded")
            await _dwell(1, 2)
            box = await page.query_selector("textarea[name='q'], input[name='q']")
            if not box:
                return
            await box.click()
            query = random.choice(_WARM_SEARCHES)
            for ch in query:
                await page.keyboard.type(ch)
                await page.wait_for_timeout(random.randint(50, 130))
            await page.keyboard.press("Enter")
            await _dwell(3, 6)
            await _scroll()
            results = await page.query_selector_all("a h3")
            if results and len(results) > 1:
                await random.choice(results[:5]).click(timeout=4000)
                await _dwell(3, 8)
                await _scroll()
        except Exception as e:
            logger.debug(f"warmer/search: {e}")

    async def _gmail():
        # Reading mail is the most direct "this is a real mailbox" signal.
        try:
            await page.goto("https://mail.google.com/mail/", timeout=25000,
                            wait_until="domcontentloaded")
            await _dwell(3, 6)
            await _scroll()
            rows = await page.query_selector_all("tr.zA, div[role='main'] tr")
            if rows and len(rows) > 1:
                await random.choice(rows[:6]).click(timeout=4000)
                await _dwell(4, 10)
                await _scroll()
        except Exception as e:
            logger.debug(f"warmer/gmail: {e}")

    async def _service():
        try:
            url = random.choice(_WARM_SERVICES)
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await _dwell(2, 5)
            await _scroll()
        except Exception as e:
            logger.debug(f"warmer/service: {e}")

    # Ordered by trust value, with a little randomness so no two runs look alike.
    plan = [_gmail, _search, _youtube, _service, _search, _gmail]
    random.shuffle(plan)

    for activity in plan:
        if time.time() >= end:
            break
        try:
            await activity()
        except Exception as e:
            logger.debug(f"warmer/activity: {e}")

    # Stop blocking assets so a later flow in this context is not starved.
    try:
        await page.unroute("**/*")
    except Exception:
        pass

    logger.info("Session warming complete")
    return True


async def warm_account_playwright(email, password, duration_minutes=3):
    """
    Warm a newly created account using Playwright.
    Logs in and simulates natural activity.
    """
    try:
        from core.pw import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — skipping account warming")
        return False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                # Must match the Client Hints the signup session presented, or the
                # warmer's login looks like a different browser than the creator's.
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # Login to Gmail
            await page.goto("https://accounts.google.com/signin", timeout=30000)
            await page.wait_for_timeout(2000)

            email_input = await page.query_selector('input[type="email"]')
            if email_input:
                await email_input.fill(email)
                await page.wait_for_timeout(500)
                await page.click('button:has-text("Next"), button:has-text("التالي")')
                await page.wait_for_timeout(3000)

            pw_input = await page.query_selector('input[type="password"]')
            if pw_input:
                await pw_input.fill(password)
                await page.wait_for_timeout(500)
                await page.click('button:has-text("Next"), button:has-text("التالي")')
                await page.wait_for_timeout(5000)

            # Check if logged in
            if "myaccount" in page.url or "mail.google" in page.url:
                logger.info(f"Successfully logged in: {email}")
            else:
                logger.warning(f"Login may have failed for {email}: {page.url}")
                await browser.close()
                return False

            start = time.time()
            target = duration_minutes * 60

            # Visit Google services
            services = [
                "https://mail.google.com/",
                "https://www.youtube.com/",
                "https://drive.google.com/",
                "https://www.google.com/",
                "https://maps.google.com/",
            ]

            while time.time() - start < target:
                url = random.choice(services)
                try:
                    await page.goto(url, timeout=15000)
                    await page.wait_for_timeout(random.randint(3000, 8000))

                    # Scroll randomly
                    await page.evaluate(f"window.scrollBy(0, {random.randint(100, 500)})")
                    await page.wait_for_timeout(random.randint(1000, 3000))
                except Exception:
                    # Without this sleep a closed page spins the loop at 100% CPU.
                    await asyncio.sleep(5)

            await browser.close()
            logger.info(f"Account warming complete for {email}")
            return True

    except Exception as e:
        logger.error(f"Account warming failed for {email}: {e}")
        return False


def warm_account_selenium(email, password, duration_minutes=3):
    """
    Warm a newly created account using Selenium.
    """
    try:
        from core.selenium_runner import create_driver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.wait import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        logger.warning("Selenium not available — skipping warming")
        return False

    driver = create_driver()
    if not driver:
        return False

    try:
        wait = WebDriverWait(driver, 15)

        # Login
        driver.get("https://accounts.google.com/signin")
        time.sleep(3)

        email_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="email"]')))
        email_input.send_keys(email)
        time.sleep(1)

        next_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Next')]")
        next_btn.click()
        time.sleep(3)

        pw_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="password"]')))
        pw_input.send_keys(password)
        time.sleep(1)

        next_btn2 = driver.find_element(By.XPATH, "//button[contains(text(), 'Next')]")
        next_btn2.click()
        time.sleep(5)

        start = time.time()
        target = duration_minutes * 60

        services = [
            "https://mail.google.com/",
            "https://www.youtube.com/",
            "https://www.google.com/",
        ]

        while time.time() - start < target:
            url = random.choice(services)
            try:
                driver.get(url)
                time.sleep(random.randint(3, 8))
                driver.execute_script(f"window.scrollBy(0, {random.randint(100, 500)})")
                time.sleep(random.randint(1, 3))
            except Exception:
                # Without this sleep a dead driver spins at 100% CPU until
                # the warming window elapses.
                time.sleep(5)

        logger.info(f"Selenium account warming complete for {email}")
        return True

    except Exception as e:
        logger.error(f"Selenium warming failed for {email}: {e}")
        return False
    finally:
        try:
            driver.quit()
        except Exception:
            pass
