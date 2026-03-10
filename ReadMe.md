MailAggregator
==============

Overview
--------
This repo contains a small IMAP-to-Gmail mail mover. It pulls new messages
from one or more source IMAP mailboxes and imports them into Gmail labels
through the Gmail API. After a successful import, the source message is deleted and
expunged. Each poll scans the current source folder and moves whatever is
still present.

Gmail Setup (per destination account)
------------------------------------
1. Create a Google Cloud project and enable the Gmail API
2. Create OAuth Desktop App credentials and save the JSON file
3. Put the credentials JSON somewhere readable and reference it from `config.ini`
4. Run the OAuth flow once so a token JSON is created for that Gmail account
   (for headless servers, use `oauth_mode=console` and `--authorize-only`)

If you want replies to use custom From addresses (info@..., support@..., etc.):
1. Gmail Settings -> Accounts and Import -> "Send mail as"
2. Add each address
3. Set "Send through your SMTP server" (MXroute SMTP)

What It Does
------------
- Near real-time inbound sync from source IMAP to Gmail
- Deletes from source only after Gmail import succeeds
- Creates destination labels if `create_labels=yes`
- Supports multiple Gmail profiles and multiple source mailboxes
- Preserves Seen/Unseen and an original-ish timestamp

What It Does Not Do
-------------------
- Sync sent mail (send from Gmail instead)
- Keep source-side history after it has been moved

Configuration Files
-------------------
`config.ini`
- General section controls polling interval and log level.
- `gmail_*` sections define each Gmail destination profile.
- `src_*` sections define each source mailbox and its destination mapping.

`config.sample.ini`
- A fully commented template you can copy to create your own `config.ini`.

`secrets.env`
- Holds source mailbox passwords as environment variables.
- `config.ini` references them via `source_password_env` to avoid storing plain text.

`*.json`
- Holds Google OAuth client credentials and per-account token files.
- Token files are refreshed automatically when possible.

Configuration Reference
-----------------------
General (`[general]`)
- `poll_seconds`: Poll interval in seconds.
- `log_level`: `DEBUG`, `INFO`, `WARNING`, or `ERROR`.
- `env_file`: Optional env file to load at startup. If relative, it is resolved from the `config.ini` directory. Defaults to `secrets.env`. For hardened systemd installs, prefer `/var/lib/mailfetcher/secrets.env`.
- `create_labels`: `yes`/`no` to auto-create destination Gmail labels.

Gmail profile (`[gmail_*]`)
- `username`: Gmail address (used for identification/logging).
- `credentials_file`: Required OAuth client JSON path. If relative, it is resolved from the `config.ini` directory.
- `token_file`: Required OAuth token JSON path. If relative, it is resolved from the `config.ini` directory.
- `oauth_mode`: `local_server` (browser-based) or `console` (copy/paste code in terminal).
- For hardened systemd installs, prefer:
  `credentials_file = /etc/mailfetcher/credentials.json`
  `token_file = /var/lib/mailfetcher/token-<profile>.json`

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

Create Python virtual environment and install dependencies:
```
sudo python3 -m venv /var/lib/mailfetcher/.venv
sudo /var/lib/mailfetcher/.venv/bin/pip install --upgrade pip
sudo /var/lib/mailfetcher/.venv/bin/pip install -r requirements.txt
sudo chown -R mailfetcher:mailfetcher /var/lib/mailfetcher/.venv
```

Install app:
```
sudo cp mailfetcher.py /usr/local/bin/mailfetcher.py
sudo chmod 755 /usr/local/bin/mailfetcher.py
sudo chown root:root /usr/local/bin/mailfetcher.py
```

Install config:
```
sudo cp config.ini /etc/mailfetcher/config.ini
sudo chown root:mailfetcher /etc/mailfetcher/config.ini
sudo chmod 640 /etc/mailfetcher/config.ini
```

Install Gmail OAuth files:
```
sudo cp credentials.json /etc/mailfetcher/credentials.json
sudo chown root:mailfetcher /etc/mailfetcher/credentials.json
sudo chmod 640 /etc/mailfetcher/credentials.json

# Create/write token location (must be writable by service user)
sudo touch /var/lib/mailfetcher/token_jpl.json
sudo chown mailfetcher:mailfetcher /var/lib/mailfetcher/token_jpl.json
sudo chmod 600 /var/lib/mailfetcher/token_jpl.json
```

Bootstrap OAuth token (headless server):
```
# In /etc/mailfetcher/config.ini under [gmail_*], set:
# oauth_mode = console

sudo -u mailfetcher -H /var/lib/mailfetcher/.venv/bin/python \
  /usr/local/bin/mailfetcher.py --authorize-only /etc/mailfetcher/config.ini
```

Install secrets:
```
sudo cp secrets.env /var/lib/mailfetcher/secrets.env
sudo chown mailfetcher:mailfetcher /var/lib/mailfetcher/secrets.env
sudo chmod 600 /var/lib/mailfetcher/secrets.env
```

Install service unit:
```
sudo cp mailfetcher.service /etc/systemd/system/mailfetcher.service
sudo systemctl daemon-reload
```

Service runtime:
- `ExecStart` runs `/var/lib/mailfetcher/.venv/bin/python` so Gmail API dependencies come from the service venv.

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
- Keep OAuth token files in `/var/lib/mailfetcher` at `chmod 600`.
- Keep `credentials.json` in `/etc/mailfetcher` at `chmod 640`.
- Prefer env var references in `config.ini` over plain text secrets.

Troubleshooting
---------------
- Authentication failures: Confirm the Gmail API is enabled, the OAuth
  credentials path is correct, and the token file matches the target account.
- `ModuleNotFoundError: No module named 'google'` in `journalctl`: the service is using
  a Python interpreter without dependencies. Recreate/update `/var/lib/mailfetcher/.venv`
  and ensure `mailfetcher.service` uses `/var/lib/mailfetcher/.venv/bin/python`.
- `could not locate runnable browser` in `journalctl`: OAuth is trying browser mode on a
  headless server. Set `oauth_mode=console`, run `--authorize-only` once interactively to
  generate the token, then restart the service.
- `Error 400: invalid_request` with `Missing required parameter: redirect_uri`: your OAuth
  client JSON is missing valid redirect URIs (or is the wrong client type). Use Google
  OAuth Desktop App credentials and update `credentials_file` in `config.ini`.
- No messages moved: Check that `source_folder` is correct and that another
  client is not moving or deleting messages before MailAggregator sees them.
- Messages not appearing in Gmail: Verify the `dest_profile` and `dest_folder`
  values and that the destination Gmail account is authorized.
- Frequent retries: Network instability or provider rate limits can cause
  backoff retries. Check source IMAP connectivity and Gmail API access from the host.
