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
5. You can also run the local token admin web UI with `--web` to renew tokens from a browser

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

`invalid-token-alert-state.txt`
- Optional state file used to rate-limit invalid-token alert emails to once per day.

Configuration Reference
-----------------------
General (`[general]`)
- `poll_seconds`: Poll interval in seconds.
- `log_level`: `DEBUG`, `INFO`, `WARNING`, or `ERROR`.
- `env_file`: Optional env file to load at startup. If relative, it is resolved from the `config.ini` directory. Defaults to `secrets.env`. For hardened systemd installs, prefer `/var/lib/mailfetcher/secrets.env`.
- `create_labels`: `yes`/`no` to auto-create destination Gmail labels.
- `token_admin_url`: Optional base URL for the token renewal site, such as `https://myserver`. When set, invalid-token alert emails include a direct renewal link.
- `invalid_token_alert_enabled`: `yes`/`no` to send a notification email when a Gmail token refresh fails because the token is no longer valid.
- `invalid_token_alert_to`: Optional fixed recipient for invalid-token alerts, such as `myself@google.com`. If omitted, MailAggregator sends the alert to the expired Gmail profile's `username`.
- `invalid_token_alert_source_section`: Optional `src_*` section to reuse for alert delivery. When set, MailAggregator uses that source account's username and stored password for SMTP. If omitted, MailAggregator reuses the first matching `src_*` mailbox for the expired Gmail profile when it can, so the alert is sent from a fetched mailbox.
- `invalid_token_alert_from`: Sender address for invalid-token alerts. Defaults to `mailfetcher@localhost`.
- `invalid_token_alert_state_file`: File used to remember whether an alert has already been sent today. Defaults to `/var/lib/mailfetcher/invalid-token-alert-state.txt` when `config.ini` is under `/etc/mailfetcher`, otherwise `invalid-token-alert-state.txt` relative to `config.ini`.
- `invalid_token_alert_smtp_host`: SMTP relay hostname for alert emails. Defaults to `localhost`.
- `invalid_token_alert_smtp_port`: SMTP relay port for alert emails. Defaults to `25`.
- `invalid_token_alert_smtp_starttls`: `yes`/`no` to enable STARTTLS before sending alert email.
- `invalid_token_alert_smtp_username`: Optional SMTP username for alert emails.
- `invalid_token_alert_smtp_password_env`: Optional env var containing the SMTP password for alert emails.
- `invalid_token_alert_smtp_password`: Optional plain-text SMTP password for alert emails (not recommended).

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
- `source_smtp_host`: Optional SMTP host for sending alerts or outbound mail. Defaults to `source_host`.
- `source_smtp_port`: Optional SMTP port for sending alerts or outbound mail. Defaults to `587`.
- `source_smtp_starttls`: `yes`/`no` to enable STARTTLS for the source account's SMTP connection. Defaults to `yes`.
- `source_smtp_from`: Optional sender address for SMTP mail. Defaults to `source_username`.
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

Run token admin web UI manually:
```
sudo -u mailfetcher -H /var/lib/mailfetcher/.venv/bin/python \
  /usr/local/bin/mailfetcher.py --web --web-host 0.0.0.0 --web-port 9941 \
  --web-base-url http://127.0.0.1 /etc/mailfetcher/config.ini
```

Then open:
```
http://127.0.0.1:9941/
http://server:9941/
http://server.local:9941/
```

The web UI lists each `gmail_*` profile and lets you:
- start a new OAuth authorization in your browser
- paste the returned Google authorization code
- save a fresh token file without using the terminal prompt

Automatic callback mode:
- For Google-compliant automatic callback, the checked-in examples use `https://myserver.com`.
- Start the web UI with `--web-base-url https://myserver.com` so OAuth uses that callback URL instead of localhost.
- Your Google OAuth client must be a `Web application` client with `https://myserver.com/oauth/callback` added as an authorized redirect URI.

Manual fallback:
- If Google rejects the redirect URI or redirects somewhere unusable, copy either the full redirected URL or just the `code` parameter value and paste it into the web form.

Install secrets:
```
sudo cp secrets.env /var/lib/mailfetcher/secrets.env
sudo chown mailfetcher:mailfetcher /var/lib/mailfetcher/secrets.env
sudo chmod 600 /var/lib/mailfetcher/secrets.env
```

Install service unit:
```
sudo cp mailfetcher.service /etc/systemd/system/mailfetcher.service
sudo cp mailfetcher-web.service /etc/systemd/system/mailfetcher-web.service
sudo systemctl daemon-reload
```

Service runtime:
- `ExecStart` runs `/var/lib/mailfetcher/.venv/bin/python` so Gmail API dependencies come from the service venv.
- `mailfetcher.service` runs the sync loop.
- `mailfetcher-web.service` binds the token renewal web UI to `0.0.0.0:9941` so you can reach it via `127.0.0.1`, `server`, and `server.local`, while OAuth callbacks use `myserver.com`.

Enable and start:
```
sudo systemctl daemon-reload
sudo systemctl enable --now mailfetcher.service
sudo systemctl enable --now mailfetcher-web.service
```

Logs:
```
sudo journalctl -u mailfetcher.service -f
sudo journalctl -u mailfetcher-web.service -f
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

To run the token admin web UI in Docker instead of the sync loop, override the command and publish a port:
```
docker compose run --service-ports --rm \
  -p 9941:9941 \
  mailfetcher /app/mailfetcher.py --web --web-host 0.0.0.0 --web-port 9941 /config/config.ini
```

HTTPS Reverse Proxy
-------------------
For Google automatic callback, use a real HTTPS hostname and a Web Application OAuth client.

1. Create a DNS name for the server, such as `mailfetcher.example.com`.
2. Put Apache, Nginx, Caddy, or another reverse proxy in front of MailAggregator.
3. Point the proxy at `http://127.0.0.1:9941`.
4. Obtain a TLS certificate for the hostname.
5. Start MailAggregator with a fixed base URL:
```
sudo -u mailfetcher -H /var/lib/mailfetcher/.venv/bin/python \
  /usr/local/bin/mailfetcher.py --web --web-host 0.0.0.0 --web-port 9941 \
  --web-base-url http://127.0.0.1 /etc/mailfetcher/config.ini
```
6. In Google Cloud Console, create or use an OAuth client of type `Web application`.
7. Add this exact redirect URI:
```
http://127.0.0.1/oauth/callback
```

Apache example:
- An example vhost is included in `mailfetcher-apache.conf`.
- Enable proxy modules:
```
sudo a2enmod proxy proxy_http headers ssl
```
- Copy the vhost file, verify `www.ds.tools`, enable the site, and reload Apache:
```
sudo cp mailfetcher-apache.conf /etc/apache2/sites-available/mailfetcher.conf
sudo nano /etc/apache2/sites-available/mailfetcher.conf
sudo a2ensite mailfetcher.conf
sudo apachectl configtest
sudo systemctl reload apache2
```
- Then add TLS with your usual Apache method, such as Certbot:
```
sudo certbot --apache -d www.ds.tools
```

Nginx example:
- An example server block is included in `mailfetcher-nginx.conf`.

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
- Google shows `invalid_request` or blocks sign-in: you are likely using a LAN hostname, plain HTTP callback, or the wrong OAuth client type. Switch to a `Web application` OAuth client with a real HTTPS callback URL such as `http://http://127.0.0.1/oauth/callback`.
- Automatic callback fails with `redirect_uri_mismatch`: add the exact HTTPS callback URL you are using to the Google OAuth client, or use the manual paste-back fallback in the web UI.
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
