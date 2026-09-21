"""
Telegram Notifier - Send notifications about account creation status
"""
import logging
import requests

logger = logging.getLogger('gmail_creator_telegram')


def _mask(secret):
    """Show only enough to identify the value, never the whole thing."""
    if not secret:
        return "***"
    s = str(secret)
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


def _mask_proxy(proxy):
    """Strip credentials from a host:port:user:pass or user:pass@host:port string."""
    if not proxy:
        return ""
    s = str(proxy)
    if "@" in s:
        _, _, hostport = s.rpartition("@")
        return hostport + " (auth hidden)"
    parts = s.split(":")
    if len(parts) >= 4:
        return f"{parts[0]}:{parts[1]} (auth hidden)"
    return s


class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None):
        from config.settings import Config
        self.bot_token = bot_token or getattr(Config, 'TELEGRAM_BOT_TOKEN', '')
        self.chat_id = chat_id or getattr(Config, 'TELEGRAM_CHAT_ID', '')
        self.enabled = bool(self.bot_token and self.chat_id)

    def send(self, message, silent=False):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

    def notify_account_created(self, email, password, strategy="", proxy=""):
        # Never ship credentials to a third party: the account password and the
        # proxy's user:pass belong in the local vault, not in a Telegram chat
        # history. v1 sent both in full.
        msg = (
            f"<b>Account Created</b>\n"
            f"<b>Email:</b> <code>{email}</code>\n"
            f"<b>Password:</b> <code>{_mask(password)}</code>\n"
        )
        if strategy:
            msg += f"<b>Strategy:</b> {strategy}\n"
        if proxy:
            msg += f"<b>Proxy:</b> {_mask_proxy(proxy)}\n"
        return self.send(msg)

    def notify_account_failed(self, username, error_type="", strategy=""):
        msg = (
            f"<b>Account Failed</b>\n"
            f"<b>Username:</b> {username}\n"
        )
        if error_type:
            msg += f"<b>Error:</b> {error_type}\n"
        if strategy:
            msg += f"<b>Strategy:</b> {strategy}\n"
        return self.send(msg, silent=True)

    def notify_batch_complete(self, total, successes, failures, duration):
        rate = (successes / total * 100) if total > 0 else 0
        msg = (
            f"<b>Batch Complete</b>\n"
            f"Total: {total} | Success: {successes} | Failed: {failures}\n"
            f"Rate: {rate:.1f}% | Duration: {duration:.0f}s"
        )
        return self.send(msg)

    def test_connection(self):
        if not self.enabled:
            return False, "Bot token or chat ID not configured"
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                bot_name = resp.json().get("result", {}).get("username", "Unknown")
                return True, f"Connected to @{bot_name}"
            return False, f"API error: {resp.status_code}"
        except Exception as e:
            return False, str(e)


notifier = TelegramNotifier()
