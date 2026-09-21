Data directory
==============

  names.txt        Name pool, one "First Last" per line. Add your own for variety.

Everything else here is runtime state, gitignored, and generated on first run:

  database.db          SQLite store (WAL mode), credentials Fernet-encrypted
  .vault.key           Fernet key, mode 0600 — the crown jewel; back it up
                       separately from the database, or not at all
  accounts.json        JSON mirror of the account table
  gmail_creator.log    Run log

The first launch migrates any plaintext passwords to the vault and writes
database.db.bak.
