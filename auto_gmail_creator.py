"""
Gmail Creator Pro v5.0 - Entry Point
Slim orchestrator that delegates to modular components in core/ and services/.
"""
import os
import sys
import random
import time
import logging

if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        def _silence_proactor():
            def safe_del(self, _orig=getattr(_ProactorBasePipeTransport, '__del__', None)):
                try:
                    if getattr(self, '_sock', None) is None:
                        return
                    if _orig:
                        _orig(self)
                except Exception:
                    pass
            _ProactorBasePipeTransport.__del__ = safe_del
        _silence_proactor()
    except ImportError:
        pass

from rich.panel import Panel
from rich.prompt import Prompt

from config.settings import Config, PROJECT_ROOT
from core.ui import (
    console, THEME, show_banner, show_menu, get_menu_choice,
    ask_num_accounts, ask_warmup_minutes, get_progress_context,
    show_dashboard, show_settings, show_accounts, show_session_summary,
    print_success, print_error, print_warning, print_info,
)
from core.account_manager import account_manager
from core.database import DatabaseManager
from core.passwords import generate_password
from core.proxy_manager import proxy_manager
from core.retry_engine import retry_engine


def setup_logging():
    if not Config.ENABLE_LOGGING:
        return
    try:
        log_dir = os.path.dirname(Config.LOG_FILE)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        log_levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
                      "WARNING": logging.WARNING, "ERROR": logging.ERROR}
        logging.basicConfig(
            filename=Config.LOG_FILE,
            level=log_levels.get(Config.LOG_LEVEL, logging.INFO),
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
    except Exception:
        pass


def run_startup_validation():
    from core.config_validator import print_validation_report
    print_validation_report(console, THEME)


from core.retry_engine import retry_engine, CreationError

# run_playwright_flow records the outcome into retry_engine but returns only a
# bool. The batch loop needs the error class itself to decide whether the proxy
# deserves the blame, so we read back the last recorded error for the flow.
_last_playwright_error = {}


class _ErrorSniffer:
    """Wrap retry_engine.record_attempt and remember the most recent error."""

    def __init__(self, real):
        self._real = real

    def __call__(self, strategy, success, error_type=None):
        if not success:
            _last_playwright_error[strategy] = error_type or CreationError.UNKNOWN
        return self._real(strategy, success, error_type)


retry_engine.record_attempt = _ErrorSniffer(retry_engine.record_attempt)

def _generate_username():
    from core.passwords import generate_name
    name = generate_name()
    parts = name.split()
    first = parts[0].lower() if parts else "user"
    last = parts[-1].lower() if len(parts) > 1 else "gmail"
    return f"{first}{last}{random.randint(1000, 9999)}", parts


def run_creation_flow(num_accounts, warmup_minutes=10, flow_mode='standard',
                 use_sms_api=False, prior_results=None):
    """
    Unified account creation flow. Routes to Playwright, Appium, or Selenium
    based on Config.ENGINE_MODE.
    """
    engine = getattr(Config, 'ENGINE_MODE', 'playwright').lower()
    password = Config.YOUR_PASSWORD

    if not password:
        try:
            with open(os.path.join(PROJECT_ROOT, "config", "password.txt"), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except FileNotFoundError:
            pass

    use_generated_passwords = not password

    successes = 0
    failures = 0
    error_counts = {}
    # A resumed session inherits its earlier tally so the summary covers the
    # whole job, not just the shard picked up after the interrupt.
    if prior_results:
        successes = int(prior_results.get("successes", 0))
        failures = int(prior_results.get("failures", 0))
        error_counts = dict(prior_results.get("error_counts", {}))
    start_time = time.time()

    with get_progress_context() as progress:
        overall = progress.add_task(f"[{THEME['success']}]Overall Progress", total=num_accounts)
        current = progress.add_task(f"[{THEME['primary']}]Current Account", total=100)

        for i in range(num_accounts):
            username, name_parts = _generate_username()
            first_name = name_parts[0] if name_parts else "User"
            last_name = name_parts[-1] if len(name_parts) > 1 else "User"

            if use_generated_passwords:
                password = generate_password()

            progress.update(current, completed=5,
                            description=f"[{THEME['primary']}]Account {i+1}/{num_accounts}...[/]")

            proxy = proxy_manager.get_best() or proxy_manager.get_next()

            success = False
            last_error = None
            attempt = 0
            max_retries = retry_engine.MAX_RETRIES if proxy_manager.count > 1 else 1

            while True:
                if attempt > 0:
                    # Only errors that implicate the egress IP cost the proxy
                    # its life. A form/validation glitch is not the proxy's
                    # fault, and killing a good residential IP for a browser
                    # crash burns the whole pool for nothing.
                    if old_proxy and retry_engine.should_change_proxy(last_error):
                        proxy_manager.mark_failure(old_proxy, fatal=True)
                        proxy = proxy_manager.get_next()
                        if proxy == old_proxy:
                            proxy = proxy_manager.get_random()
                    # A taken username is not reusable; everything else retries
                    # with a fresh identity to avoid colliding with Google's
                    # cache of the failed attempt.
                    if last_error != CreationError.USERNAME_TAKEN:
                        username, name_parts = _generate_username()
                        first_name = name_parts[0] if name_parts else "User"
                        last_name = name_parts[-1] if len(name_parts) > 1 else "User"
                        if use_generated_passwords:
                            password = generate_password()
                    progress.update(current, completed=5,
                                    description=f"[{THEME['warning']}]Retry {attempt} — Account {i+1}...[/]")
                    time.sleep(retry_engine.get_cooldown(attempt - 1, last_error))

                old_proxy = proxy
                try:
                    if engine == 'playwright':
                        from core.runners import run_playwright_flow
                        result = run_playwright_flow(
                            i, num_accounts, username, first_name, last_name,
                            password, progress, current, proxy,
                            use_sms_api=use_sms_api, flow_mode=flow_mode,
                        )
                        error_type = _last_playwright_error.get(flow_mode)
                    elif engine == 'appium':
                        from core.runners import run_appium_flow
                        month, day, year = Config.YOUR_BIRTHDAY.split() if Config.YOUR_BIRTHDAY else ("1", "1", "1990")
                        result = run_appium_flow(
                            i, num_accounts, username, first_name, last_name,
                            password, month, day, year, str(Config.YOUR_GENDER),
                            progress, current,
                        )
                        error_type = None
                    else:
                        from core.selenium_runner import run_selenium_flow
                        result = run_selenium_flow(
                            i, num_accounts, username, password,
                            warmup_minutes=warmup_minutes,
                            stealth_mode=(not use_sms_api),
                            mode=flow_mode, proxy=proxy,
                        )
                        error_type = None
                    success = bool(result)
                    if not success and error_type is None:
                        error_type = CreationError.UNKNOWN
                    last_error = error_type
                except Exception as e:
                    print_error(f"Account {i+1} error: {e}")
                    success = False
                    last_error = CreationError.BROWSER_CRASH

                if success:
                    break
                attempt += 1
                if not retry_engine.should_retry(last_error, attempt) or attempt >= max_retries:
                    break

            if success:
                successes += 1
                retry_engine.record_attempt(flow_mode, True)
                error_counts[CreationError.USERNAME_TAKEN] = error_counts.get(CreationError.USERNAME_TAKEN, 0)
                print_success(f"Account {i+1}/{num_accounts}: {username}@gmail.com CREATED")
                if proxy:
                    proxy_manager.mark_success(proxy)
            else:
                failures += 1
                retry_engine.record_attempt(flow_mode, False, last_error)
                error_counts[last_error or CreationError.UNKNOWN] = (
                    error_counts.get(last_error or CreationError.UNKNOWN, 0) + 1
                )
                print_error(f"Account {i+1}/{num_accounts}: {username}@gmail.com FAILED ({last_error or 'unknown'})")
                # A failure the proxy did not cause still marks it suspect:
                # mark_failure() only downweights, it does not blacklist.
                if proxy:
                    proxy_manager.mark_failure(proxy)

            progress.update(overall, advance=1)
            progress.update(current, completed=0)

            # Save session state for resume capability
            try:
                from core.session_resume import session_manager
                completed_indices = list(range(i + 1))
                session_manager.save_state(
                    batch_config={"num_accounts": num_accounts, "flow_mode": flow_mode,
                                  "use_sms_api": use_sms_api, "warmup_minutes": warmup_minutes},
                    completed_indices=completed_indices,
                    results={"successes": successes, "failures": failures,
                             "error_counts": error_counts},
                )
            except Exception:
                pass

            if i < num_accounts - 1:
                delay = getattr(Config, 'DELAY_BETWEEN_ACCOUNTS', 30)
                time.sleep(delay)

    duration = time.time() - start_time
    show_session_summary(num_accounts, successes, failures, duration)

    db = DatabaseManager()
    db.save_session_stats(
        total_attempts=num_accounts, successes=successes, failures=failures,
        strategies_used={flow_mode: num_accounts}, errors=error_counts,
        duration_seconds=duration,
    )

    # Telegram batch notification
    try:
        from core.telegram_notifier import notifier
        notifier.notify_batch_complete(num_accounts, successes, failures, duration)
    except Exception:
        pass

    # Clear saved session on completion
    try:
        from core.session_resume import session_manager
        session_manager.clear_state()
    except Exception:
        pass


def handle_proxy_test():
    print_info("Testing proxy connectivity...")
    from core.trust_builder import network_trust_check
    info = network_trust_check()
    if info and info.get("is_datacenter"):
        print_warning(f"Datacenter IP detected: {info['ip']} ({info['city']}, {info['country']})")
        print_warning("Use a residential proxy for better results.")
    elif info:
        print_success(f"IP: {info['ip']} ({info['city']}, {info['country']}) - Residential")
    else:
        print_warning("Could not determine IP info")

    results = proxy_manager.check_all_health()
    total = results['healthy'] + results['unhealthy']
    print_info(f"Proxy health: {results['healthy']}/{total} proxies healthy")


def handle_export(choice, accounts):
    if choice == "1":
        path = account_manager.export_csv()
        print_success(f"Exported to {path}")
    elif choice == "2":
        path = account_manager.export_json()
        print_success(f"Exported to {path}")
    elif choice == "3":
        path = account_manager.export_txt()
        print_success(f"Exported to {path}")


def main():
    setup_logging()

    os.makedirs(os.path.join(PROJECT_ROOT, "data"), exist_ok=True)

    run_startup_validation()

    db = DatabaseManager()
    migrated = db.run_migration()
    if migrated > 0:
        print_info(f"Migrated {migrated} accounts from old format to SQLite")

    # Check for saved session on startup
    try:
        from core.session_resume import session_manager
        if session_manager.has_saved_session():
            print_warning("A previously interrupted session was found.")
    except Exception:
        pass

    engine_name = getattr(Config, 'ENGINE_MODE', 'playwright').upper()

    try:
        while True:
            show_banner()
            show_menu(engine_name=engine_name)
            choice = get_menu_choice()

            if choice == "1":
                # GHOST MODE - Free bypass
                num = ask_num_accounts()
                warmup = ask_warmup_minutes()

                console.print(Panel(
                    f"[{THEME['success']}]GHOST MODE - 100% FREE (ENGINE: {engine_name})[/]\n"
                    f"[{THEME['primary']}]No phone API required. Maximum Stealth.[/]\n\n"
                    f"[{THEME['warning']}]Accounts: {num} | Warmup: {warmup} min[/]",
                    border_style=THEME['success'],
                    title="GHOST MODE",
                ))
                run_creation_flow(num, warmup_minutes=warmup, use_sms_api=False)

            elif choice == "2":
                # PREMIUM MODE - With SMS API
                num = ask_num_accounts()
                has_sms = any([
                    Config.FIVESIM_API_KEY, Config.SMS_ACTIVATE_API_KEY,
                    Config.ONLINESIM_API_KEY, getattr(Config, 'GETSMS_API_KEY', ''),
                    getattr(Config, 'VAKSMS_EMAIL', ''),
                ])
                if not has_sms:
                    print_warning("No SMS API keys configured! Set at least one in .env")
                    confirm = Prompt.ask(f"[{THEME['warning']}]Continue anyway?[/]",
                                         choices=["y", "n"], default="n")
                    if confirm != "y":
                        continue

                console.print(Panel(
                    f"[{THEME['secondary']}]PREMIUM MODE (ENGINE: {engine_name})[/]\n"
                    f"[{THEME['primary']}]With SMS API verification[/]\n\n"
                    f"[{THEME['warning']}]Accounts: {num}[/]",
                    border_style=THEME['secondary'],
                    title="PREMIUM MODE",
                ))
                run_creation_flow(num, warmup_minutes=0, use_sms_api=True)

            elif choice == "3":
                # DASHBOARD
                stats = account_manager.get_stats()
                retry_stats = retry_engine.get_stats()
                proxy_stats = proxy_manager.get_stats()
                show_dashboard(stats, retry_stats, proxy_stats)

            elif choice == "4":
                # CONFIGURATION
                show_settings()

            elif choice == "5":
                # SAVED ACCOUNTS
                accounts = account_manager.get_all()
                export_choice = show_accounts(accounts)
                if export_choice and export_choice != "0":
                    handle_export(export_choice, accounts)

            elif choice == "6":
                # NETWORK TEST
                handle_proxy_test()
                input("\nPress Enter to continue...")

            elif choice == "7":
                # YOUTUBE GHOST MODE
                num = ask_num_accounts()
                console.print(Panel(
                    f"[{THEME['warning']}]YOUTUBE GHOST MODE[/]\n"
                    f"Bypassing standard flows via YouTube signup...",
                    border_style=THEME['warning'],
                    title="YOUTUBE GHOST",
                ))
                run_creation_flow(num, warmup_minutes=0, flow_mode="youtube", use_sms_api=False)

            elif choice == "8":
                # WORKSPACE GHOST MODE
                num = ask_num_accounts()
                console.print(Panel(
                    f"[{THEME['warning']}]WORKSPACE GHOST MODE[/]\n"
                    f"Bypassing standard flows via Workspace signup...",
                    border_style=THEME['warning'],
                    title="WORKSPACE GHOST",
                ))
                run_creation_flow(num, warmup_minutes=0, flow_mode="workspace", use_sms_api=False)

            elif choice == "9":
                # HEALTH CHECK
                accounts = account_manager.get_all()
                if not accounts:
                    print_warning("No accounts found to check")
                else:
                    from core.health_checker import AccountHealthChecker
                    print_info(f"Checking health of {len(accounts)} accounts via IMAP...")
                    results = AccountHealthChecker.check_all(accounts)
                    summary = AccountHealthChecker.get_summary(results)
                    console.print(Panel(
                        f"[{THEME['success']}]Active: {summary['active']}[/]\n"
                        f"[{THEME['warning']}]Locked: {summary['locked']}[/]\n"
                        f"[{THEME['error']}]Suspended: {summary['suspended']}[/]\n"
                        f"Password Changed: {summary['password_changed']}\n"
                        f"Errors: {summary['errors']}\n"
                        f"\n[bold]Health Rate: {summary['health_rate']:.1f}%[/]",
                        title=f"Health Check Results ({summary['total']} accounts)",
                        border_style=THEME['primary'],
                    ))

            elif choice == "10":
                # PROXY INFO — public proxy scraping was removed: free proxies are
                # datacenter IPs that guarantee phone verification on signup.
                print_warning(
                    "Public proxy scraping is gone — free proxies are datacenter IPs "
                    "that force phone verification.\nAdd residential/mobile proxies to "
                    "config/proxies.txt manually (one per line)."
                )

            elif choice == "11":
                # TELEGRAM TEST
                from core.telegram_notifier import notifier
                if not notifier.enabled:
                    print_warning("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
                else:
                    ok, msg = notifier.test_connection()
                    if ok:
                        print_success(f"Telegram connected: {msg}")
                        notifier.send("Gmail Creator Pro: Test notification")
                        print_success("Test message sent!")
                    else:
                        print_error(f"Telegram connection failed: {msg}")

            elif choice == "12":
                # RESUME SESSION
                from core.session_resume import session_manager
                if not session_manager.has_saved_session():
                    print_warning("No saved session found to resume")
                else:
                    state = session_manager.load_state()
                    if state:
                        remaining = session_manager.get_remaining(state)
                        cfg = state.get("batch_config", {})
                        prev = state.get("results", {})
                        console.print(Panel(
                            f"[{THEME['primary']}]Saved at: {state.get('saved_at', 'Unknown')}[/]\n"
                            f"Total accounts: {cfg.get('num_accounts', 0)}\n"
                            f"Completed: {len(state.get('completed_indices', []))}\n"
                            f"Remaining: {len(remaining)}\n"
                            f"Previous successes: {prev.get('successes', 0)} | failures: {prev.get('failures', 0)}",
                            title="Saved Session",
                            border_style=THEME['warning'],
                        ))
                        confirm = Prompt.ask(f"[{THEME['primary']}]Resume this session?[/]",
                                             choices=["y", "n"], default="y")
                        if confirm == "y":
                            # Replay the prior tally so the resumed batch
                            # reports on the whole job, not just this shard.
                            run_creation_flow(
                                len(remaining),
                                warmup_minutes=cfg.get("warmup_minutes", 5),
                                flow_mode=cfg.get("flow_mode", "standard"),
                                use_sms_api=cfg.get("use_sms_api", False),
                                prior_results=prev,
                            )
                        else:
                            clear_confirm = Prompt.ask(
                                f"[{THEME['warning']}]Clear saved session?[/]",
                                choices=["y", "n"], default="n")
                            if clear_confirm == "y":
                                session_manager.clear_state()
                                print_info("Session cleared")

            elif choice == "0":
                console.print(f"\n[{THEME['primary']}]{'='*60}[/]")
                console.print(f"[bold bright_white]  SHADOW GENESIS[/] [{THEME['secondary']}]v{THEME['version']}[/] [{THEME['primary']}]| Shutting Down...[/]")
                console.print(f"[{THEME['primary']}]{'='*60}[/]\n")
                break

            try:
                Prompt.ask(f"\n[{THEME['primary']}]Press Enter to continue...[/]", default="")
            except Exception:
                pass

    except KeyboardInterrupt:
        console.print(f"\n[{THEME['warning']}]Process interrupted by user.[/]")
    except Exception as e:
        console.print(f"\n[{THEME['error']}]Fatal error:[/]")
        console.print(f"[{THEME['error']}]{e!r}[/]")

    console.print(f"\n[{THEME['primary']}]{'='*74}[/]")
    console.print(f"[bold bright_white]  SHADOW GENESIS[/] [{THEME['secondary']}]v{THEME['version']}[/] [{THEME['primary']}]|[/] [dim]Developed by[/] [bold bright_white]Shadow Hacker[/]")
    console.print(f"  [dim]All Rights Reserved[/] [{THEME['primary']}]|[/] [{THEME['secondary']}]Phantom Core Engine[/]")
    console.print(f"[{THEME['primary']}]{'='*74}[/]\n")


if __name__ == "__main__":
    main()
