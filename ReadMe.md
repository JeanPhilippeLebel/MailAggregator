MailAggregator
==============

Overview
--------
This repo contains a small IMAP-to-Gmail mail mover. It pulls new messages
from one or more source IMAP mailboxes and appends them into Gmail folders
(labels). After a successful append, the source message is deleted and
expunged. It keeps a local SQLite DB with UIDVALIDITY and the last copied UID
so it only moves new mail on the next poll.

Gmail Setup (per destination account)
------------------------------------
1. Gmail Settings -> Forwarding and POP/IMAP -> enable IMAP
2. Turn on 2FA
3. Create an app password for "Mail"
4. Put that app password in `secrets.env` and reference it in `config.ini`

If you want replies to use custom From addresses (info@..., support@..., etc.):
1. Gmail Settings -> Accounts and Import -> "Send mail as"
2. Add each address
3. Set "Send through your SMTP server" (MXroute SMTP)

What It Does
------------
- Near real-time inbound sync from source IMAP to Gmail
- Deletes from source only after Gmail append succeeds
- Creates destination labels if `create_labels=yes`
- Supports multiple Gmail profiles and multiple source mailboxes
- Preserves Seen/Unseen and an original-ish timestamp

What It Does Not Do
-------------------
- Sync sent mail (send from Gmail instead)
- Backfill old history unless UIDs are reset

Configuration Files
-------------------
`config.ini`
- General section controls polling interval, log level, state DB path.
- `gmail_*` sections define each Gmail destination profile.
- `src_*` sections define each source mailbox and its destination mapping.

`config.sample.ini`
- A fully commented template you can copy to create your own `config.ini`.

`secrets.env`
- Holds app passwords and source passwords as environment variables.
- `config.ini` references them via `*_env` keys to avoid storing plain text.

Configuration Reference
-----------------------
General (`[general]`)
- `poll_seconds`: Poll interval in seconds.
- `state_db`: Path to SQLite DB used to track UIDVALIDITY and last UID.
- `log_level`: `DEBUG`, `INFO`, `WARNING`, or `ERROR`.
- `create_labels`: `yes`/`no` to auto-create destination Gmail labels.

Gmail profile (`[gmail_*]`)
- `host`: IMAP host (typically `imap.gmail.com`).
- `port`: IMAP SSL port (typically `993`).
- `username`: Gmail address.
- `app_password_env`: Env var name holding the Gmail app password.
- `app_password`: Optional plain-text fallback (not recommended).

Source mailbox (`[src_*]`)
- `source_host`: Source IMAP host.
- `source_port`: Source IMAP SSL port.
- `source_username`: Source mailbox user.
- `source_password_env`: Env var name holding the source password.
- `source_password`: Optional plain-text fallback (not recommended).
- `source_folder`: Folder to pull from (default `INBOX`).
- `dest_profile`: Gmail profile name to receive mail.
- `dest_folder`: Comma-separated list of Gmail labels to apply. If empty, defaults to the source email address.
  Include `\Inbox` to force a message to appear in Inbox. System labels (like `\Inbox`) are not auto-created.

Local/Service Install (systemd)
-------------------------------
Create system user and directories:
```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin mailfetcher || true
sudo mkdir -p /etc/mailfetcher
sudo mkdir -p /var/lib/mailfetcher
sudo chown -R mailfetcher:mailfetcher /var/lib/mailfetcher
sudo chmod 700 /var/lib/mailfetcher
```

Install config:
```
sudo cp config.ini /etc/mailfetcher/config.ini
sudo chmod 600 /etc/mailfetcher/config.ini
sudo chown root:root /etc/mailfetcher/config.ini
```

Enable and start:
```
sudo systemctl daemon-reload
sudo systemctl enable --now mailfetcher.service
```

Logs:
```
sudo journalctl -u mailfetcher.service -f
```

Docker
------
Run:
```
docker compose up -d --build
docker compose logs -f
```

Stop:
```
docker compose down
```

Security Notes
--------------
- Keep `secrets.env` at `chmod 600`.
- Prefer env var references in `config.ini` over plain text secrets.

Troubleshooting
---------------
- Authentication failures: Confirm IMAP is enabled in Gmail and the app password
  is referenced correctly in `config.ini` and exported in `secrets.env`.
- No new messages: Check that `source_folder` is correct and that the last UID
  in the state DB is not ahead of the mailbox. If you need a reset, delete the
  state DB and restart.
- Messages not appearing in Gmail: Verify the `dest_profile` and `dest_folder`
  values and that the destination Gmail account is connected.
- Frequent retries: Network instability or provider rate limits can cause
  backoff retries. Check IMAP connectivity from the host.
