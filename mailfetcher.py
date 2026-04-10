#!/usr/bin/env python3
"""
Mail fetcher:
- Connects to one or more source IMAP mailboxes.
- Imports new messages into Gmail via the Gmail API.
- Applies Gmail labels from config; defaults to per-source label (source email address).
- Deletes from source only after a successful Gmail import.
"""
import base64
import configparser
import datetime
import email.policy
import imaplib
import logging
import os
import signal
import smtplib
import sys
import time
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

STOP = False
SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
]
SYSTEM_LABEL_MAP = {
    "\\Inbox": "INBOX",
    "\\Important": "IMPORTANT",
    "\\Starred": "STARRED",
    "\\Sent": "SENT",
    "\\Draft": "DRAFT",
    "\\Drafts": "DRAFT",
    "\\Spam": "SPAM",
    "\\Trash": "TRASH",
    "\\Unread": "UNREAD",
}
KNOWN_SYSTEM_LABEL_IDS = {
    "INBOX",
    "IMPORTANT",
    "STARRED",
    "SENT",
    "DRAFT",
    "SPAM",
    "TRASH",
    "UNREAD",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}


class GmailInvalidAttachmentError(RuntimeError):
    pass


class InvalidGmailTokenError(RuntimeError):
    pass


def handle_signal(signum, frame):
    # Allow clean shutdown between poll cycles and between message copies.
    global STOP
    STOP = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def require_env(var_name, section_name):
    # Fail fast if a required env var is missing.
    val = os.environ.get(var_name, "")
    if not val:
        raise RuntimeError("Missing env var %s (needed by section %s)" % (var_name, section_name))
    return val


def get_secret_from_env(cp, section_name, env_key_name, fallback_plain_key=None):
    # Resolve a secret from env var reference; optionally allow plain text for dev.
    env_var_name = cp.get(section_name, env_key_name, fallback="").strip()
    if env_var_name:
        return require_env(env_var_name, section_name)

    if fallback_plain_key:
        plain = cp.get(section_name, fallback_plain_key, fallback="").strip()
        if plain:
            return plain

    raise RuntimeError("Missing secret reference in section %s (expected %s)" % (section_name, env_key_name))


def resolve_path(config_dir, raw_value):
    raw = (raw_value or "").strip()
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(config_dir, raw))


def ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def save_gmail_token(token_file, creds):
    ensure_parent_dir(token_file)
    with open(token_file, "w", encoding="utf-8") as token:
        token.write(creds.to_json())


def require_config_value(cp, section_name, key_name):
    value = cp.get(section_name, key_name, fallback="").strip()
    if not value:
        raise RuntimeError("Missing required config value %s in section %s" % (key_name, section_name))
    return value


def parse_bool(raw_value, default=False):
    clean = str(raw_value or "").strip().lower()
    if not clean:
        return default
    return clean in ("1", "yes", "true", "on")


def get_optional_secret_from_env(cp, section_name, env_key_name, fallback_plain_key=None):
    env_var_name = cp.get(section_name, env_key_name, fallback="").strip()
    if env_var_name:
        return require_env(env_var_name, section_name)

    if fallback_plain_key:
        plain = cp.get(section_name, fallback_plain_key, fallback="").strip()
        if plain:
            return plain

    return ""


def default_invalid_token_alert_state_file(config_dir):
    clean_dir = os.path.abspath(config_dir)
    if clean_dir == "/etc/mailfetcher":
        return "/var/lib/mailfetcher/invalid-token-alert-state.txt"
    return os.path.join(clean_dir, "invalid-token-alert-state.txt")


def load_invalid_token_alert_config(cp, config_dir):
    section_name = "general"
    enabled = parse_bool(cp.get(section_name, "invalid_token_alert_enabled", fallback="no"))
    recipient = cp.get(section_name, "invalid_token_alert_to", fallback="").strip()
    state_file = resolve_path(
        config_dir,
        cp.get(
            section_name,
            "invalid_token_alert_state_file",
            fallback=default_invalid_token_alert_state_file(config_dir),
        ),
    )
    return {
        "enabled": enabled and bool(recipient),
        "to": recipient,
        "source_section": cp.get(section_name, "invalid_token_alert_source_section", fallback="").strip(),
        "from": cp.get(section_name, "invalid_token_alert_from", fallback="mailfetcher@localhost").strip(),
        "smtp_host": cp.get(section_name, "invalid_token_alert_smtp_host", fallback="localhost").strip(),
        "smtp_port": int(cp.get(section_name, "invalid_token_alert_smtp_port", fallback="25")),
        "smtp_starttls": parse_bool(cp.get(section_name, "invalid_token_alert_smtp_starttls", fallback="no")),
        "smtp_username": cp.get(section_name, "invalid_token_alert_smtp_username", fallback="").strip(),
        "smtp_password": get_optional_secret_from_env(
            cp,
            section_name,
            "invalid_token_alert_smtp_password_env",
            fallback_plain_key="invalid_token_alert_smtp_password",
        ),
        "state_file": state_file,
    }


def enrich_invalid_token_alert_config_from_source(cp, alert_cfg):
    source_section = alert_cfg.get("source_section", "")
    if not source_section:
        return alert_cfg

    if not cp.has_section(source_section):
        raise RuntimeError(
            "invalid_token_alert_source_section=%s does not match any config section" % source_section
        )

    smtp_username = cp.get(source_section, "source_username", fallback="").strip()
    smtp_password = get_secret_from_env(
        cp,
        source_section,
        "source_password_env",
        fallback_plain_key="source_password",
    )
    smtp_host = cp.get(source_section, "source_smtp_host", fallback="").strip() or cp.get(
        source_section, "source_host", fallback=""
    ).strip()
    smtp_port = int(cp.get(source_section, "source_smtp_port", fallback="587"))
    smtp_starttls = parse_bool(cp.get(source_section, "source_smtp_starttls", fallback="yes"))
    sender = cp.get(source_section, "source_smtp_from", fallback="").strip() or smtp_username

    merged = dict(alert_cfg)
    merged["from"] = sender or merged.get("from", "")
    merged["smtp_host"] = smtp_host or merged.get("smtp_host", "")
    merged["smtp_port"] = smtp_port
    merged["smtp_starttls"] = smtp_starttls
    merged["smtp_username"] = smtp_username or merged.get("smtp_username", "")
    merged["smtp_password"] = smtp_password
    return merged


def invalid_token_alert_sent_today(alert_cfg):
    if not alert_cfg.get("enabled"):
        return False

    try:
        with open(alert_cfg["state_file"], "r", encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except FileNotFoundError:
        return False
    except Exception as e:
        logging.warning("Could not read invalid-token alert state file %s: %s", alert_cfg["state_file"], str(e))
        return False


def mark_invalid_token_alert_sent_today(alert_cfg):
    ensure_parent_dir(alert_cfg["state_file"])
    with open(alert_cfg["state_file"], "w", encoding="utf-8") as f:
        f.write(datetime.date.today().isoformat())


def send_invalid_token_alert_once_per_day(alert_cfg, profile_name, error_text):
    if not alert_cfg.get("enabled"):
        return

    if invalid_token_alert_sent_today(alert_cfg):
        logging.info("Invalid-token alert already sent today; skipping additional notification")
        return

    msg = EmailMessage()
    msg["To"] = alert_cfg["to"]
    msg["From"] = alert_cfg["from"]
    msg["Subject"] = "MailFetcher Gmail token invalid for %s" % profile_name
    msg.set_content(
        "MailFetcher detected an invalid Gmail token.\n\n"
        "Profile: %s\n"
        "Date: %s\n"
        "Error: %s\n"
        % (
            profile_name,
            datetime.datetime.now().astimezone().isoformat(),
            error_text,
        )
    )

    try:
        with smtplib.SMTP(alert_cfg["smtp_host"], alert_cfg["smtp_port"], timeout=30) as smtp:
            if alert_cfg.get("smtp_starttls"):
                smtp.starttls(context=ssl_create_default_context())
            if alert_cfg.get("smtp_username"):
                smtp.login(alert_cfg["smtp_username"], alert_cfg.get("smtp_password", ""))
            smtp.send_message(msg)
        mark_invalid_token_alert_sent_today(alert_cfg)
        logging.info("Sent invalid-token alert email to %s", alert_cfg["to"])
    except Exception as e:
        logging.error("Failed to send invalid-token alert email: %s", str(e))


def load_env_file(env_path):
    if not env_path or not os.path.exists(env_path):
        return False

    loaded = 0
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value
            loaded += 1

    logging.info("Loaded %d env var(s) from %s", loaded, env_path)
    return True


def imap_login(host, port, username, password, timeout=30):
    # Login with SSL and a socket timeout to avoid hanging indefinitely.
    logging.info("IMAP login: connecting to %s:%s as %s", host, port, username)
    ctx = ssl_create_default_context()
    try:
        c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
    except TypeError:
        # Fallback for older Python versions without IMAP4_SSL(timeout=...).
        import socket

        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        finally:
            socket.setdefaulttimeout(prev_timeout)
    typ, data = c.login(username, password)
    if typ != "OK":
        raise RuntimeError("IMAP login failed for %s: %s" % (username, data))
    logging.info("IMAP login: authenticated as %s on %s:%s", username, host, port)
    return c


def ssl_create_default_context():
    import ssl

    return ssl.create_default_context()


def imap_select(c, folder):
    # Select folder (read/write) for message fetch and delete operations.
    logging.info("Selecting folder %s", folder)
    typ, data = c.select('"%s"' % folder, readonly=False)
    if typ != "OK":
        raise RuntimeError("SELECT failed for folder %s: %s" % (folder, data))
    return data


def _parse_search_data(data):
    if not data or not data[0]:
        return []
    out = []
    for p in data[0].split():
        try:
            out.append(int(p))
        except Exception:
            pass
    return out


def imap_search_messages(c):
    # Refresh the current message list each time to avoid relying on stale ids.
    typ, data = c.search(None, "UNDELETED")
    if typ != "OK":
        raise RuntimeError("SEARCH UNDELETED failed: %s" % (data,))
    return _parse_search_data(data)


def imap_fetch_rfc822(c, msg_num):
    # Fetch full message, flags, internal date and Date header.
    logging.debug("Fetching message %s", msg_num)
    typ, data = c.fetch(str(msg_num), "(RFC822 FLAGS INTERNALDATE)")
    if typ != "OK" or not data:
        raise RuntimeError("FETCH failed for message %s: %s" % (msg_num, data))

    raw_msg = None
    flags = []
    internaldate = None
    header_date = None

    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            meta = item[0].decode("utf-8", errors="ignore")
            blob = item[1]
            if blob and "RFC822" in meta:
                raw_msg = blob

            if "FLAGS" in meta:
                i = meta.find("FLAGS")
                if i >= 0:
                    j = meta.find(")", i)
                    k = meta.find("(", i)
                    if k >= 0 and j >= 0 and j > k:
                        fstr = meta[k + 1 : j].strip()
                        flags = [x for x in fstr.split() if x.startswith("\\")]

            if "INTERNALDATE" in meta:
                i = meta.find("INTERNALDATE")
                if i >= 0:
                    q1 = meta.find('"', i)
                    q2 = meta.find('"', q1 + 1) if q1 >= 0 else -1
                    if q1 >= 0 and q2 > q1:
                        internaldate = meta[q1 + 1 : q2]

    if raw_msg is None:
        raise RuntimeError("RFC822 body missing for message %s" % msg_num)

    try:
        head = raw_msg.split(b"\r\n\r\n", 1)[0]
        for line in head.decode("utf-8", errors="ignore").splitlines():
            if line.lower().startswith("date:"):
                header_date = line[5:].strip()
                break
    except Exception:
        pass

    return raw_msg, flags, internaldate, header_date


def is_message_gone_error(err):
    # Detect common IMAP responses meaning a searched message disappeared before FETCH/STORE.
    s = str(err).lower()
    markers = [
        "fetch failed for message",
        "rfc822 body missing for message",
        "store +deleted failed for message",
    ]
    if not any(m in s for m in markers):
        return False

    gone_hints = [
        "no such message",
        "not found",
        "invalid message set",
        "no matching",
        "nonexistent",
        "expunge",
    ]
    return any(h in s for h in gone_hints)


def parse_imap_internaldate_to_datetime(internaldate_str):
    # Parse IMAP INTERNALDATE into a timezone-aware datetime.
    if not internaldate_str:
        return None
    try:
        return datetime.datetime.strptime(internaldate_str, "%d-%b-%Y %H:%M:%S %z")
    except Exception:
        return None


def choose_append_date(internaldate_str, header_date_str):
    # Prefer INTERNALDATE; fall back to Date header or current local time.
    dt = parse_imap_internaldate_to_datetime(internaldate_str)
    if dt is not None:
        return dt

    if header_date_str:
        try:
            dt = parsedate_to_datetime(header_date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
            return dt
        except Exception:
            pass

    return datetime.datetime.now().astimezone()


def parse_dest_labels(dest_folder_value, default_label):
    # dest_folder can be a comma-separated list of labels.
    # If empty, fall back to the default label (source email address).
    raw = (dest_folder_value or "").strip()
    if not raw:
        return [default_label] if default_label else []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def oauth_authorize(profile_name, credentials_file, oauth_mode):
    mode = (oauth_mode or "local_server").strip().lower()
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)

    if mode == "local_server":
        return flow.run_local_server(port=0)

    if mode == "console":
        if not sys.stdin.isatty():
            raise RuntimeError(
                "[%s] OAuth console mode requires an interactive terminal to paste the auth code" % profile_name
            )
        if not flow.redirect_uri:
            redirect_uris = []
            # google-auth-oauthlib versions may expose either:
            # - full client config ({installed:{...}} / {web:{...}})
            # - direct installed/web subsection ({client_id:..., redirect_uris:[...]})
            client_cfg = flow.client_config or {}
            installed_cfg = client_cfg.get("installed", {}) if isinstance(client_cfg, dict) else {}
            web_cfg = client_cfg.get("web", {}) if isinstance(client_cfg, dict) else {}
            if isinstance(client_cfg, dict):
                redirect_uris.extend(client_cfg.get("redirect_uris", []) or [])
            if isinstance(installed_cfg, dict):
                redirect_uris.extend(installed_cfg.get("redirect_uris", []) or [])
            if isinstance(web_cfg, dict):
                redirect_uris.extend(web_cfg.get("redirect_uris", []) or [])
            if not redirect_uris:
                # Desktop app default; keeps console flow working in headless setups.
                redirect_uris.append("http://localhost")
            if redirect_uris:
                flow.redirect_uri = redirect_uris[0]
        if not flow.redirect_uri:
            raise RuntimeError(
                "[%s] OAuth credentials file has no redirect_uris. "
                "Use Google OAuth Desktop App credentials JSON."
                % profile_name
            )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        print("")
        print("[%s] Open this URL in your browser and authorize access:" % profile_name)
        print(auth_url)
        print(
            "[%s] After approval, if the browser redirects to localhost and errors, "
            "copy the 'code' parameter value from the URL."
            % profile_name
        )
        code = input("[%s] Enter the authorization code: " % profile_name).strip()
        if not code:
            raise RuntimeError("[%s] Empty authorization code" % profile_name)
        flow.fetch_token(code=code)
        return flow.credentials

    raise RuntimeError("[%s] Invalid oauth_mode '%s' (expected local_server or console)" % (profile_name, mode))


def gmail_api_login(profile_name, profile):
    creds = None
    token_file = profile["token_file"]
    credentials_file = profile["credentials_file"]

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            logging.warning("[%s] Could not load token file %s: %s", profile_name, token_file, str(e))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.info("[%s] Refreshing Gmail API token", profile_name)
            try:
                creds.refresh(Request())
            except Exception as e:
                raise InvalidGmailTokenError(
                    "[%s] Gmail token refresh failed. The stored token likely has the wrong scopes. "
                    "Delete %s and re-authorize with scopes: %s. Original error: %s"
                    % (profile_name, token_file, ", ".join(SCOPES), str(e))
                )
        else:
            if not os.path.exists(credentials_file):
                raise RuntimeError("[%s] Missing credentials file: %s" % (profile_name, credentials_file))
            logging.info("[%s] Starting OAuth flow using %s", profile_name, credentials_file)
            oauth_mode = profile.get("oauth_mode", "local_server")
            try:
                creds = oauth_authorize(profile_name, credentials_file, oauth_mode)
            except Exception as e:
                mode = (oauth_mode or "local_server").strip().lower()
                if mode == "local_server" and sys.stdin.isatty():
                    logging.warning(
                        "[%s] local_server OAuth failed (%s); falling back to console OAuth mode",
                        profile_name,
                        str(e),
                    )
                    creds = oauth_authorize(profile_name, credentials_file, "console")
                else:
                    raise RuntimeError(
                        "[%s] OAuth authorization failed in mode=%s: %s. "
                        "On headless servers, set oauth_mode=console in config and run "
                        "'mailfetcher.py --authorize-only /path/to/config.ini' from an interactive terminal."
                        % (profile_name, mode, str(e))
                    )
        save_gmail_token(token_file, creds)

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    client = {
        "service": service,
        "profile_name": profile_name,
        "username": profile.get("username") or "me",
        "labels_by_name": None,
        "labels_by_id": set(),
    }
    gmail_refresh_label_cache(client)
    return client


def gmail_refresh_label_cache(gmail_client):
    response = gmail_client["service"].users().labels().list(userId="me").execute()
    labels_by_name = {}
    labels_by_id = set()
    for label in response.get("labels", []):
        name = label.get("name")
        label_id = label.get("id")
        if name and label_id:
            labels_by_name[name] = label_id
            labels_by_id.add(label_id)
    gmail_client["labels_by_name"] = labels_by_name
    gmail_client["labels_by_id"] = labels_by_id


def resolve_system_label_id(label_name):
    clean = str(label_name or "").strip()
    if not clean:
        return None
    if clean in SYSTEM_LABEL_MAP:
        return SYSTEM_LABEL_MAP[clean]
    upper = clean.upper()
    if upper in KNOWN_SYSTEM_LABEL_IDS:
        return upper
    return None


def gmail_lookup_label_id(gmail_client, label_name):
    if gmail_client["labels_by_name"] is None:
        gmail_refresh_label_cache(gmail_client)
    return gmail_client["labels_by_name"].get(label_name)


def gmail_create_label_if_needed(gmail_client, label_name):
    system_label_id = resolve_system_label_id(label_name)
    if system_label_id:
        return system_label_id

    existing = gmail_lookup_label_id(gmail_client, label_name)
    if existing:
        return existing

    try:
        created = gmail_client["service"].users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 409:
            gmail_refresh_label_cache(gmail_client)
            existing = gmail_lookup_label_id(gmail_client, label_name)
            if existing:
                return existing
        raise RuntimeError("Gmail label create failed for %s: %s" % (label_name, str(e)))

    label_id = created.get("id")
    if not label_id:
        raise RuntimeError("Gmail label create returned no id for %s" % label_name)
    gmail_client["labels_by_name"][label_name] = label_id
    gmail_client["labels_by_id"].add(label_id)
    return label_id


def build_gmail_label_ids(gmail_client, configured_labels, source_flags, create_labels):
    label_ids = []
    seen = set()

    def add_label_id(label_id):
        if label_id and label_id not in seen:
            seen.add(label_id)
            label_ids.append(label_id)

    add_label_id("UNREAD")

    for label_name in configured_labels or []:
        system_label_id = resolve_system_label_id(label_name)
        if system_label_id:
            add_label_id(system_label_id)
            continue

        label_id = gmail_lookup_label_id(gmail_client, label_name)
        if not label_id:
            if not create_labels:
                raise RuntimeError("Destination label %s does not exist and create_labels=no" % label_name)
            label_id = gmail_create_label_if_needed(gmail_client, label_name)
        add_label_id(label_id)

    return label_ids


def normalize_message_date(raw_msg, append_dt):
    # Gmail API can only derive internal date from the message Date header.
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(raw_msg)
        formatted = format_datetime(append_dt)
        if "Date" in msg:
            msg.replace_header("Date", formatted)
        else:
            msg["Date"] = formatted
        return msg.as_bytes(policy=email.policy.SMTP)
    except Exception as e:
        logging.warning("Could not normalize Date header for Gmail import: %s", str(e))
        return raw_msg


def datetime_to_gmail_internal_date_ms(dt):
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return str(int(dt.timestamp() * 1000))


def is_invalid_attachment_http_error(err):
    if not isinstance(err, HttpError):
        return False
    if getattr(err.resp, "status", None) != 400:
        return False
    return "Invalid attachment" in str(err)


def gmail_import_message(gmail_client, raw_msg, flags, append_dt, labels, create_labels):
    label_ids = build_gmail_label_ids(gmail_client, labels, flags, create_labels)
    prepared_msg = normalize_message_date(raw_msg, append_dt)
    encoded_prepared = base64.urlsafe_b64encode(prepared_msg).decode("ascii")
    body = {"raw": encoded_prepared, "labelIds": label_ids}

    try:
        result = gmail_client["service"].users().messages().import_(
            userId="me",
            body=body,
            internalDateSource="dateHeader",
        ).execute()
    except HttpError as e:
        if not is_invalid_attachment_http_error(e):
            raise

        logging.warning(
            "[%s] Gmail import rejected message as invalid attachment; retrying with messages.insert",
            gmail_client["profile_name"],
        )
        fallback_body = {
            "raw": base64.urlsafe_b64encode(raw_msg).decode("ascii"),
            "labelIds": label_ids,
            "internalDate": datetime_to_gmail_internal_date_ms(append_dt),
        }
        result = gmail_client["service"].users().messages().insert(
            userId="me",
            body=fallback_body,
            internalDateSource="receivedTime",
        ).execute()
    except HttpError as e:
        if is_invalid_attachment_http_error(e):
            raise GmailInvalidAttachmentError(str(e))
        raise

    if not result.get("id"):
        raise RuntimeError("Gmail API import returned no message id")
    logging.debug("Gmail API import ok id=%s labels=%s", result.get("id"), label_ids)
    return True


def source_delete_message(source_conn, msg_num):
    # Mark message deleted on source; actual delete happens on EXPUNGE.
    logging.debug("Marking source message %s as deleted", msg_num)
    typ, data = source_conn.store(str(msg_num), "+FLAGS.SILENT", "(\\Deleted)")
    if typ != "OK":
        raise RuntimeError("STORE +Deleted failed for message %s: %s" % (msg_num, data))


def source_expunge(source_conn):
    # Permanently remove messages marked as \Deleted.
    logging.info("Expunging source mailbox")
    typ, data = source_conn.expunge()
    if typ != "OK":
        raise RuntimeError("EXPUNGE failed: %s" % (data,))


def load_config(path):
    # Load INI config, keeping section names as-is.
    logging.info("Loading config from %s", path)
    cp = configparser.ConfigParser()
    with open(path, "r", encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def iter_source_sections(cp):
    # Source sections are named "src_*".
    for sec in cp.sections():
        if sec.startswith("src_"):
            yield sec


def close_source_quietly(src):
    try:
        src.logout()
    except Exception:
        pass


def main():
    raw_args = sys.argv[1:]
    authorize_only = False
    if "--authorize-only" in raw_args:
        authorize_only = True
        raw_args = [a for a in raw_args if a != "--authorize-only"]

    if len(raw_args) != 1:
        print("Usage: mailfetcher.py [--authorize-only] /path/to/config.ini")
        return 2

    config_path = os.path.abspath(raw_args[0])
    config_dir = os.path.dirname(config_path)
    cp = load_config(config_path)
    env_file_path = resolve_path(
        config_dir,
        cp.get("general", "env_file", fallback="secrets.env"),
    )

    poll_seconds = int(cp.get("general", "poll_seconds", fallback="60"))
    log_level = cp.get("general", "log_level", fallback="INFO").upper()
    create_labels = cp.get("general", "create_labels", fallback="yes").lower() in ("1", "yes", "true", "on")
    imap_timeout_seconds = int(cp.get("general", "imap_timeout_seconds", fallback="60"))
    invalid_token_alert_cfg = load_invalid_token_alert_config(cp, config_dir)

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.info("Starting mailfetcher")
    logging.info(
        "Settings: poll_seconds=%s log_level=%s create_labels=%s imap_timeout_seconds=%s",
        poll_seconds,
        log_level,
        create_labels,
        imap_timeout_seconds,
    )
    load_env_file(env_file_path)
    invalid_token_alert_cfg = enrich_invalid_token_alert_config_from_source(cp, invalid_token_alert_cfg)

    gmail_profiles = {}
    for sec in cp.sections():
        if sec.startswith("gmail_"):
            gmail_profiles[sec] = {
                "username": cp.get(sec, "username", fallback=""),
                "credentials_file": resolve_path(
                    config_dir,
                    require_config_value(cp, sec, "credentials_file"),
                ),
                "token_file": resolve_path(
                    config_dir,
                    require_config_value(cp, sec, "token_file"),
                ),
                "oauth_mode": cp.get(sec, "oauth_mode", fallback="local_server"),
            }

    if not gmail_profiles:
        logging.error("No gmail_* profiles in config")
        return 2

    logging.info("Configured %d Gmail profile(s)", len(gmail_profiles))
    source_sections = list(iter_source_sections(cp))
    logging.info("Configured %d source mailbox section(s)", len(source_sections))

    if authorize_only:
        logging.info("Authorize-only mode: authenticating Gmail profile(s) and writing token file(s)")
        failed_profiles = 0
        for name, g in gmail_profiles.items():
            try:
                gmail_api_login(name, g)
                logging.info(
                    "Authorized Gmail profile %s using token=%s",
                    name,
                    g["token_file"],
                )
            except Exception as e:
                failed_profiles += 1
                logging.error("[%s] Authorization failed: %s", name, str(e))
                if isinstance(e, InvalidGmailTokenError):
                    send_invalid_token_alert_once_per_day(invalid_token_alert_cfg, name, str(e))
        logging.info("Authorize-only mode complete")
        return 1 if failed_profiles else 0

    while not STOP:
        loop_start = time.time()
        gmail_conns = {}
        try:
            logging.info("Starting poll cycle")
            for name, g in gmail_profiles.items():
                try:
                    gmail_conns[name] = gmail_api_login(name, g)
                    logging.info(
                        "Connected Gmail profile %s using token=%s",
                        name,
                        g["token_file"],
                    )
                except Exception as e:
                    logging.error("[%s] Gmail login failed; skipping profile for this cycle: %s", name, str(e))
                    if isinstance(e, InvalidGmailTokenError):
                        send_invalid_token_alert_once_per_day(invalid_token_alert_cfg, name, str(e))

            if not gmail_conns:
                logging.error("No Gmail profiles authenticated successfully this cycle")
                continue

            for sec in source_sections:
                if STOP:
                    break

                shost = cp.get(sec, "source_host")
                sport = int(cp.get(sec, "source_port", fallback="993"))
                suser = cp.get(sec, "source_username")
                spass = get_secret_from_env(cp, sec, "source_password_env", fallback_plain_key="source_password")
                sfolder = cp.get(sec, "source_folder", fallback="INBOX")
                dprofile = cp.get(sec, "dest_profile")
                dfolder = cp.get(sec, "dest_folder", fallback="")

                if dprofile not in gmail_conns:
                    logging.error("[%s] dest_profile %s not found", sec, dprofile)
                    continue

                logging.info(
                    "[%s] Source=%s:%s user=%s folder=%s -> dest=%s labels=%s",
                    sec,
                    shost,
                    sport,
                    suser,
                    sfolder,
                    dprofile,
                    dfolder or suser,
                )

                backoff = 1
                for attempt in range(1, 6):
                    src = None
                    try:
                        logging.info("[%s] Attempt %d/5: connecting source %s", sec, attempt, suser)
                        src = imap_login(shost, sport, suser, spass, timeout=imap_timeout_seconds)
                        imap_select(src, sfolder)
                        pending = imap_search_messages(src)
                        if not pending:
                            logging.info("[%s] No messages to move", sec)
                            close_source_quietly(src)
                            break

                        logging.info("[%s] Found %d message(s) ready to move", sec, len(pending))
                        gmail = gmail_conns[dprofile]
                        dest_labels = parse_dest_labels(dfolder, suser)

                        if create_labels and dest_labels:
                            for lbl in dest_labels:
                                if resolve_system_label_id(lbl):
                                    continue
                                label_id = gmail_create_label_if_needed(gmail, lbl)
                                logging.info(
                                    "[%s] Destination label %s ready (id=%s)",
                                    sec,
                                    lbl,
                                    label_id,
                                )

                        moved = 0
                        skipped_invalid_attachments = set()
                        while not STOP:
                            msg_nums = imap_search_messages(src)
                            msg_num = None
                            for candidate in msg_nums:
                                if candidate not in skipped_invalid_attachments:
                                    msg_num = candidate
                                    break
                            if msg_num is None:
                                break

                            try:
                                logging.info("[%s] Copying message %d", sec, msg_num)
                                raw_msg, flags, internaldate_str, header_date_str = imap_fetch_rfc822(src, msg_num)
                                append_dt = choose_append_date(internaldate_str, header_date_str)
                                gmail_import_message(gmail, raw_msg, flags, append_dt, dest_labels, create_labels)
                                source_delete_message(src, msg_num)
                                moved += 1
                            except Exception as msg_err:
                                if isinstance(msg_err, GmailInvalidAttachmentError):
                                    skipped_invalid_attachments.add(msg_num)
                                    logging.warning(
                                        "[%s] Skipping message %d because Gmail rejected an attachment: %s",
                                        sec,
                                        msg_num,
                                        str(msg_err),
                                    )
                                elif is_message_gone_error(msg_err):
                                    logging.warning(
                                        "[%s] Message %d disappeared before copy/delete; skipping (%s)",
                                        sec,
                                        msg_num,
                                        str(msg_err),
                                    )
                                else:
                                    raise

                        if moved > 0:
                            source_expunge(src)
                            logging.info(
                                "[%s] Moved %d messages to %s (labels: %s)",
                                sec,
                                moved,
                                dprofile,
                                ", ".join(dest_labels) if dest_labels else "(none)",
                            )

                        close_source_quietly(src)
                        break

                    except Exception as e:
                        close_source_quietly(src)
                        logging.warning("[%s] Attempt %d failed: %s", sec, attempt, str(e))
                        logging.info("[%s] Sleeping %ds before retry", sec, backoff)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                else:
                    logging.error("[%s] Exhausted retries after 5 attempts", sec)

            logging.info("Poll cycle complete")

        except Exception as e:
            logging.error("Loop error: %s", str(e))

        elapsed = time.time() - loop_start
        sleep_s = max(1, poll_seconds - int(elapsed))
        logging.info("Sleeping %ds before next poll", sleep_s)
        for _ in range(sleep_s):
            if STOP:
                break
            time.sleep(1)

    logging.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
