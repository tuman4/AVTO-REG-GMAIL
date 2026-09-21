Configuration directory
=======================

  settings.py      Config class; every value comes from .env (see .env.example)
  proxies.txt      Proxy pool, one per line (gitignored — contains credentials)
                  Formats: host:port | host:port:user:pass
                           user:pass@host:port | socks5://host:port

All other configuration lives in .env at the project root. Copy .env.example
to .env and fill in your keys.

proxies.txt is gitignored on purpose. Never commit it.
