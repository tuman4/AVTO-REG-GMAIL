"""
SecureVault - Fernet symmetric encryption for stored credentials.

Implements what the README has always advertised: AES-128-CBC + HMAC-SHA256
via cryptography's Fernet token format, with a persisted key reused across
sessions.

Key handling:
  - data/.vault.key holds a urlsafe Fernet key, mode 0600 where the OS allows it.
  - The key is generated once and reused; losing it makes stored passwords
    unrecoverable, which is the intended property.
  - If the key is missing or corrupted, the vault re-initializes and legacy
    plaintext rows are re-encrypted on next save/read.

Fallback: if `cryptography` is unavailable, the vault degrades to plaintext and
logs a loud warning at every write rather than silently storing secrets —
"silently plaintext" is what got v1 in trouble.
"""
import os
import logging

logger = logging.getLogger('gmail_creator_vault')

_VAULT_KEY_PATH = os.path.join("data", ".vault.key")

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception
    _CRYPTO_AVAILABLE = False
    logger.warning(
        "cryptography package is not installed — SecureVault is storing "
        "credentials in PLAINTEXT. Run: pip install cryptography"
    )


class SecureVault:
    _instance = None
    _fernet = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_key()
        return cls._instance

    def _init_key(self):
        if not _CRYPTO_AVAILABLE:
            return
        os.makedirs(os.path.dirname(_VAULT_KEY_PATH) or ".", exist_ok=True)
        try:
            if os.path.exists(_VAULT_KEY_PATH):
                with open(_VAULT_KEY_PATH, "rb") as f:
                    key = f.read().strip()
                # Validate: a bad key must not silently break encryption.
                Fernet(key)
                self._fernet = Fernet(key)
                return
        except (ValueError, OSError, InvalidToken) as e:
            logger.warning(f"Vault key unusable, regenerating: {e}")
        key = Fernet.generate_key()
        try:
            fd = os.open(_VAULT_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        except OSError as e:
            logger.warning(f"Could not restrict vault key permissions: {e}")
            with open(_VAULT_KEY_PATH, "wb") as f:
                f.write(key)
        self._fernet = Fernet(key)

    @property
    def enabled(self):
        return _CRYPTO_AVAILABLE and self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a secret. Returns a Fernet token string, or the plaintext
        itself only when encryption is genuinely unavailable (with a warning)."""
        if not plaintext:
            return plaintext
        if not self.enabled:
            logger.warning("SecureVault disabled — storing credential in plaintext")
            return plaintext
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a stored value. Transparently passes through legacy plaintext
        rows so old data keeps working after the vault is introduced."""
        if not token:
            return token
        if not self.enabled:
            return token
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            # Legacy plaintext row from before the vault existed — return as-is.
            return token

    def is_encrypted(self, value: str) -> bool:
        """Fernet tokens always start with this prefix; plaintext does not."""
        return bool(value) and value.startswith("gAAAAA")


vault = SecureVault()
