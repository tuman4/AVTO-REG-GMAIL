# Security policy

## Reporting a vulnerability

Email the maintainers directly rather than opening a public issue. Include a
reproduction case and, if applicable, the affected file and line numbers.

## Disclosure

We credit reporters unless they prefer to remain anonymous.

## Security posture of this software

**This tool creates accounts against Google's public signup flow. It is not
malware** — it installs no persistence, touches no system outside its own
working directory, and exfiltrates nothing. Its security concerns are about
protecting *the operator's* secrets, not attacking anyone else.

### Credential storage

- Account passwords are encrypted at rest with Fernet (symmetric AES-128-CBC
  + HMAC-SHA256).
- The encryption key lives at `data/.vault.key` with `0600` permissions.
- **The key is the crown jewel.** Anyone with both the database and the key
  owns every stored account. Store backups of the two separately, or do not
  back up the key at all — Google's own account recovery makes passwords
  recoverable without it.
- The original build stored passwords in plaintext. The first launch
  re-encrypts them automatically and writes `data/database.db.bak`.

### What must never be committed

`.gitignore` excludes all of these — verify before pushing:

| Path | Why |
|---|---|
| `.env` | API keys, Telegram tokens, proxy credentials |
| `data/.vault.key` | The Fernet key |
| `data/database.db*` | Encrypted credentials |
| `data/accounts.json` | Account dump |
| `config/proxies.txt` | Proxy credentials |
| `*.log` | May contain masked-but-sensitive output |

`.env.example` is the only environment file that ships, and it contains
placeholders only.

### Network exposure

- `services/voice.py` binds a local HTTP server. **It refuses to start without
  `VOICE_SERVER_TOKEN`** — an empty token is a hard failure, not an open
  endpoint. The original build exposed `/otp` unauthenticated.
- Telegram notifications mask passwords and proxy credentials before sending.

### Third-party services

SMS, captcha, and proxy providers receive your payment details and traffic.
Use a dedicated virtual card and rotate API keys periodically. This project
never sends your credentials anywhere except the provider's own API.

### Responsible use

Automating account creation violates Google's Terms of Service. Using created
accounts for spam, fraud, phishing, or harassing real people is illegal and
unacceptable. This tool exists for security research, testing, and education.
