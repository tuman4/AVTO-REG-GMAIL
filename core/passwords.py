"""
Password and identity generation — kept dependency-free so the Playwright path
never has to import selenium just to create a password or pick a name.
"""
import random
import secrets
import string

from config.settings import Config


def generate_password(length: int = 14) -> str:
    """A strong unique password per account: 3 upper, 5 lower, 3 digits, 2 specials."""
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


def _load_names() -> list[str]:
    names_file = getattr(Config, "NAMES_FILE", "data/names.txt")
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


def generate_name() -> str:
    if _names_list:
        return random.choice(_names_list)
    return f"User{random.randint(1000, 9999)}"
