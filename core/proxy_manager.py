"""
Proxy Manager - Advanced proxy rotation and health checking
"""
import os
import time
import random
import logging
import threading
import requests
from urllib.parse import quote, unquote, urlsplit
from config.settings import Config

logger = logging.getLogger('gmail_creator_proxy')

# Proxy schemes we accept on a single line. Anything not listed is treated as HTTP.
_SUPPORTED_SCHEMES = ("http://", "https://", "socks5://", "socks4://")

_HEALTH_URL = "https://www.google.com/robots.txt"
_RECOVERY_SCORE = 25  # a blacklisted proxy is re-tested once the pool runs dry


class ProxyManager:
    def __init__(self):
        self._proxies = []
        self._current_index = 0
        self._health = {}
        self._scores = {}
        self._lock = threading.RLock()
        self._load_proxies()

    def _load_proxies(self):
        proxy_file = Config.PROXY_FILE
        if not os.path.exists(proxy_file):
            logger.warning(f"Proxy file not found: {proxy_file}")
            return
        with open(proxy_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                normalized = self.normalize(line)
                if not normalized:
                    logger.warning(f"Skipping malformed proxy line: {line}")
                    continue
                if normalized not in self._proxies:
                    self._proxies.append(normalized)
                    self._health[normalized] = True
                    self._scores[normalized] = 50
        if self._proxies:
            logger.info(f"Loaded {len(self._proxies)} proxies")

    @staticmethod
    def normalize(line: str):
        """Accept documented formats without losing scheme or IPv6 brackets."""
        parsed = ProxyManager.parse(line)
        if not parsed:
            return None

        if parsed["scheme"].startswith("socks") and not requests_builtins_support_socks():
            logger.warning(
                f"SOCKS proxy {parsed['host']}:{parsed['port']} requires PySocks"
            )

        host = parsed["host"]
        host_for_url = f"[{host}]" if ":" in host else host
        prefix = "" if parsed["scheme"] == "http" else f"{parsed['scheme']}://"
        canonical = f"{prefix}{host_for_url}:{parsed['port']}"
        if parsed["user"]:
            canonical += f":{parsed['user']}:{parsed['pass'] or ''}"
        return canonical

    @property
    def count(self):
        return len(self._proxies)

    @property
    def healthy_count(self):
        with self._lock:
            return sum(1 for p in self._proxies if self._health.get(p, True))

    def _healthy(self):
        """Healthy list, rebuilt atomically under the lock."""
        with self._lock:
            return [p for p in self._proxies if self._health.get(p, True)]

    def get_random(self):
        healthy = self._healthy()
        if not healthy:
            return None
        return random.choice(healthy)

    def get_next(self):
        """Round-robin over healthy proxies — always advances, never repeats back-to-back."""
        with self._lock:
            healthy = self._healthy()
            if not healthy:
                return None
            idx = self._current_index % len(healthy)
            self._current_index = (self._current_index + 1) % max(len(healthy), 1)
            return healthy[idx]

    def get_best(self):
        """Weighted random pick among healthy proxies so a fresh list actually rotates."""
        healthy = self._healthy()
        if not healthy:
            return None
        with self._lock:
            weights = [max(self._scores.get(p, 50), 1) for p in healthy]
        return random.choices(healthy, weights=weights, k=1)[0]

    def mark_success(self, proxy):
        with self._lock:
            if proxy in self._scores:
                self._scores[proxy] = min(100, self._scores[proxy] + 10)
                self._health[proxy] = True

    def mark_failure(self, proxy, fatal=False):
        with self._lock:
            if proxy in self._scores:
                self._scores[proxy] = max(0, self._scores[proxy] - (30 if fatal else 10))
                if self._scores[proxy] <= 10:
                    self._health[proxy] = False
                    logger.warning(f"Proxy marked unhealthy: {self._mask(proxy)}")

    def _mask(self, proxy):
        """Never write proxy credentials to logs."""
        parsed = self.parse(proxy)
        if not parsed:
            return proxy
        if parsed["user"]:
            return f"{parsed['host']}:{parsed['port']}:***:***"
        return f"{parsed['host']}:{parsed['port']}"

    def check_health(self, proxy, timeout=10):
        parsed = self.parse(proxy)
        if not parsed:
            return False
        try:
            proxy_url = self._proxy_url(parsed)
            proxies_dict = {"http": proxy_url, "https": proxy_url}
            resp = requests.get(_HEALTH_URL, proxies=proxies_dict, timeout=timeout)
            try:
                if resp.status_code == 200:
                    with self._lock:
                        self._health[proxy] = True
                    return True
            finally:
                resp.close()
        except Exception as e:
            logger.debug(f"Proxy health check failed for {self._mask(proxy)}: {e}")
        with self._lock:
            self._health[proxy] = False
        return False

    def check_all_health(self):
        results = {"healthy": 0, "unhealthy": 0}
        for proxy in self._proxies:
            if self.check_health(proxy):
                results["healthy"] += 1
            else:
                results["unhealthy"] += 1
        return results

    def _requests_proxies(self, proxy):
        parsed = self.parse(proxy)
        if not parsed:
            return None
        url = self._proxy_url(parsed)
        return {"http": url, "https": url}

    @staticmethod
    def _proxy_url(parsed):
        host = f"[{parsed['host']}]" if ":" in parsed["host"] else parsed["host"]
        auth = ""
        if parsed["user"] is not None:
            auth = quote(parsed["user"], safe="")
            if parsed["pass"] is not None:
                auth += ":" + quote(parsed["pass"], safe="")
            auth += "@"
        return f"{parsed['scheme']}://{auth}{host}:{parsed['port']}"

    def get_ip_info(self, proxy=None):
        try:
            proxies_dict = self._requests_proxies(proxy) if proxy else {}

            ip_resp = requests.get("https://api.ipify.org?format=json", proxies=proxies_dict, timeout=10)
            try:
                ip = ip_resp.json().get("ip", "Unknown")
            finally:
                ip_resp.close()

            info_resp = requests.get(
                f"https://ipinfo.io/{ip}/json",
                proxies=proxies_dict,
                timeout=10,
            )
            try:
                info = info_resp.json()
            finally:
                info_resp.close()

            is_datacenter = "hosting" in str(info.get("org", "")).lower()
            return {
                "ip": ip,
                "city": info.get("city", "N/A"),
                "country": info.get("country", "N/A"),
                "org": info.get("org", "N/A"),
                "is_datacenter": is_datacenter,
            }
        except Exception as e:
            logger.warning(f"IP info check failed: {e}")
            return None

    def rotate_mobile_ip(self):
        url = getattr(Config, 'MOBILE_PROXY_IP_CHANGE_URL', '')
        if not url:
            return False
        try:
            logger.info("Rotating mobile proxy IP...")
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                wait_time = getattr(Config, 'PROXY_CHANGE_WAIT_TIME', 10)
                logger.info(f"IP changed. Waiting {wait_time}s for propagation...")
                time.sleep(wait_time)
                return True
            logger.warning(f"IP rotation returned status {resp.status_code}")
        except Exception as e:
            logger.error(f"Mobile IP rotation failed: {e}")
        return False

    @staticmethod
    def parse(proxy_string):
        if not proxy_string:
            return None
        s = str(proxy_string).strip()
        scheme = "http"
        for supported in _SUPPORTED_SCHEMES:
            if s.lower().startswith(supported):
                scheme = supported[:-3]
                s = s[len(supported):]
                break
        else:
            if "://" in s:
                return None

        # Legacy host:port:user:pass is still accepted, including when the
        # host is not an IPv6 literal.
        if "@" not in s and not s.startswith("[") and s.count(":") == 3:
            host, port, user, pwd = s.split(":", 3)
            candidate = f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
        else:
            candidate = f"{scheme}://{s}"

        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None

        if not host or port is None or not (1 <= port <= 65535):
            return None

        return {
            "scheme": scheme,
            "host": unquote(host),
            "port": str(port),
            "user": unquote(parsed.username) if parsed.username is not None else None,
            "pass": unquote(parsed.password) if parsed.password is not None else None,
        }

    @staticmethod
    def format_for_playwright(proxy_string):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        host = f"[{parsed['host']}]" if ":" in parsed["host"] else parsed["host"]
        result = {"server": f"{parsed['scheme']}://{host}:{parsed['port']}"}
        if parsed["user"]:
            result["username"] = parsed["user"]
            result["password"] = parsed["pass"] or ""
        return result

    @staticmethod
    def format_for_selenium(proxy_string, proxy_type="http"):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        host = f"[{parsed['host']}]" if ":" in parsed["host"] else parsed["host"]
        if parsed["user"]:
            return f"{proxy_type}://{parsed['user']}:{parsed['pass'] or ''}@{host}:{parsed['port']}"
        return f"{host}:{parsed['port']}"

    def maybe_recover_blacklisted(self):
        """Give blacklisted proxies a second chance when nothing healthy is left."""
        with self._lock:
            for p in self._proxies:
                if not self._health.get(p, True) and self._scores.get(p, 0) <= 10:
                    self._scores[p] = _RECOVERY_SCORE
                    self._health[p] = True
                    logger.info(f"Proxy {self._mask(p)} re-queued for retry (pool exhausted)")

    def get_next_or_recover(self):
        proxy = self.get_next() or self.get_best()
        if proxy is None:
            self.maybe_recover_blacklisted()
            proxy = self.get_next()
        return proxy

    def get_stats(self):
        with self._lock:
            return {
                "total": len(self._proxies),
                "healthy": self.healthy_count,
                "unhealthy": len(self._proxies) - self.healthy_count,
                "scores": {self._mask(p): self._scores.get(p, 0) for p in self._proxies[:10]},
            }


def requests_builtins_support_socks():
    try:
        import socks  # noqa: F401
        return True
    except ImportError:
        return False


proxy_manager = ProxyManager()
