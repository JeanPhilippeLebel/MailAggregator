#!/usr/bin/env python3
"""
Mail fetcher:
- Connects to one or more source IMAP mailboxes.
- Appends new messages into Gmail via IMAP APPEND.
- Applies Gmail labels from config; defaults to per-source label (source email address).
- Deletes from source only after a successful append+label.
- Tracks UIDVALIDITY/last UID per mailbox in a local sqlite DB.
"""
import configparser
import imaplib
import logging
import os
import signal
import sqlite3
import ssl
import sys
import time
import datetime
import re
from email.utils import parsedate_to_datetime

STOP = False


def handle_signal(signum, frame):
    # Allow clean shutdown between poll cycles and between message copies.
    global STOP
    STOP = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def now_ts():
    # Unix epoch seconds for DB timestamps.
    return int(time.time())


def ensure_dir(path):
    # Create parent directory for a file path if it does not exist.
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


class StateDB:
    def __init__(self, path):
        # Persist mailbox sync state locally so we only copy new mail.
        ensure_dir(path)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox_state (
                mailbox_id TEXT PRIMARY KEY,
                uidvalidity INTEGER NOT NULL,
                last_uid INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def get(self, mailbox_id):
        # Read UIDVALIDITY + last UID for a mailbox id.
        cur = self.conn.execute(
            "SELECT uidvalidity, last_uid FROM mailbox_state WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"uidvalidity": int(row[0]), "last_uid": int(row[1])}

    def put(self, mailbox_id, uidvalidity, last_uid):
        # Upsert current UIDVALIDITY + last UID.
        self.conn.execute(
            """
            INSERT INTO mailbox_state (mailbox_id, uidvalidity, last_uid, updated_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(mailbox_id) DO UPDATE SET
                uidvalidity=excluded.uidvalidity,
                last_uid=excluded.last_uid,
                updated_ts=excluded.updated_ts
            """,
            (mailbox_id, int(uidvalidity), int(last_uid), now_ts()),
        )
        self.conn.commit()


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


def imap_login(host, port, username, password, timeout=30):
    # Login with SSL and a socket timeout to avoid hanging indefinitely.
    logging.info("IMAP login: connecting to %s:%s as %s", host, port, username)
    ctx = ssl.create_default_context()
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


def imap_select(c, folder):
    # Select folder (read/write) for UID and FETCH operations.
    logging.info("Selecting folder %s", folder)
    typ, data = c.select('"%s"' % folder, readonly=False)
    if typ != "OK":
        raise RuntimeError("SELECT failed for folder %s: %s" % (folder, data))
    return data


def imap_get_uidvalidity(c, folder):
    # UIDVALIDITY changes mean UIDs can no longer be trusted (RFC 3501).
    typ, data = c.response("UIDVALIDITY")
    if typ == "OK" and data and data[0]:
        try:
            return int(data[0])
        except Exception:
            pass

    typ, data = c.status('"%s"' % folder, "(UIDVALIDITY)")
    if typ == "OK" and data:
        for item in data:
            if not item:
                continue
            if isinstance(item, bytes):
                m = re.search(rb"UIDVALIDITY\s+(\d+)", item)
            else:
                m = re.search(r"UIDVALIDITY\s+(\d+)", str(item))
            if m:
                return int(m.group(1))

    raise RuntimeError("Could not read UIDVALIDITY")


def imap_create_folder_if_needed(c, folder):
    # Create a Gmail label (folder) if configured; no-op if it already exists.
    typ, _ = c.create('"%s"' % folder)
    if typ == "OK":
        return True
    typ2, data2 = c.list(directory='""', pattern='"%s"' % folder)
    if typ2 == "OK" and data2:
        return True
    return False


def _parse_uid_search_data(data):
    if not data or not data[0]:
        return []
    out = []
    for p in data[0].split():
        try:
            out.append(int(p))
        except Exception:
            pass
    return out


def imap_get_max_uid(c):
    # Best-effort: get the max existing UID in the currently selected mailbox.
    typ, data = c.uid("SEARCH", None, "ALL")
    if typ != "OK":
        typ2, data2 = c.search(None, "ALL")
        if typ2 != "OK":
            return None
        uids = _parse_uid_search_data(data2)
    else:
        uids = _parse_uid_search_data(data)

    if not uids:
        return 0
    return max(uids)


def imap_search_uids(c, last_uid):
    # Some servers reject "UID SEARCH UID <range>" as invalid; when using UID command,
    # the search criteria should already be UID-based (no "UID" search key).
    q = "%d:*" % (last_uid + 1)
    logging.debug("Searching UIDs with UID SEARCH query: %s", q)
    typ, data = c.uid("SEARCH", None, q)
    if typ == "OK":
        return _parse_uid_search_data(data)

    # Fallback for servers that reject UID SEARCH but accept SEARCH UID ...
    logging.warning("UID SEARCH failed (%s); falling back to SEARCH UID", data)
    q2 = "UID %d:*" % (last_uid + 1)
    typ2, data2 = c.search(None, q2)
    if typ2 != "OK":
        raise RuntimeError("UID SEARCH failed: %s; SEARCH UID failed: %s" % (data, data2))
    return _parse_uid_search_data(data2)


def imap_fetch_rfc822(c, uid):
    # Fetch full message, flags, internal date and Date header.
    logging.debug("Fetching message UID %s", uid)
    typ, data = c.uid("FETCH", str(uid), "(RFC822 FLAGS INTERNALDATE)")
    if typ != "OK" or not data:
        raise RuntimeError("FETCH failed for uid %s: %s" % (uid, data))

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
        raise RuntimeError("RFC822 body missing for uid %s" % uid)

    # Extract Date header from the fetched full message as a fallback timestamp source.
    try:
        head = raw_msg.split(b"\r\n\r\n", 1)[0]
        for line in head.decode("utf-8", errors="ignore").splitlines():
            if line.lower().startswith("date:"):
                header_date = line[5:].strip()
                break
    except Exception:
        pass

    return raw_msg, flags, internaldate, header_date


def parse_imap_internaldate_to_datetime(internaldate_str):
    # Parse IMAP INTERNALDATE into a timezone-aware datetime for APPEND.
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

def gmail_select_folder(gmail_conn, folder):
    # STORE requires SELECTED state on many IMAP servers.
    current = getattr(gmail_conn, "_selected_folder", None)
    if current == folder:
        return True
    typ, data = gmail_conn.select('"%s"' % folder, readonly=False)
    if typ != "OK":
        raise RuntimeError("Gmail SELECT failed for %s: %s" % (folder, data))
    gmail_conn._selected_folder = folder
    return True

def _parse_appenduid(data):
    if not data:
        return None
    for item in data:
        if not item:
            continue
        if isinstance(item, bytes):
            s = item.decode("utf-8", errors="ignore")
        else:
            s = str(item)
        m = re.search(r"APPENDUID\s+(\d+)\s+(\d+)", s)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def _decode_list_line(x):
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="ignore")
    return str(x)


def gmail_find_all_mail_folder(gmail_conn):
    """
    Try to find the server's "All Mail" mailbox name.
    Returns a mailbox name string, or None if not found.
    """
    typ, data = gmail_conn.list()
    if typ != "OK" or not data:
        return None

    candidates = []
    for line in data:
        s = _decode_list_line(line)

        # Prefer IMAP attributes that indicate All Mail (\All).
        # Example: (\HasNoChildren \All) "/" "[Gmail]/All Mail"
        if re.search(r"\(.*\\All.*\)", s, flags=re.IGNORECASE):
            m = re.findall(r'"([^"]+)"\s*$', s)
            if m:
                return m[-1]
            parts = s.split(" ")
            if parts:
                return parts[-1].strip()

        # Take the last quoted string as mailbox name if present
        m = re.findall(r'"([^"]+)"\s*$', s)
        if m:
            mbox = m[-1]
        else:
            parts = s.split(" ")
            mbox = parts[-1].strip()

        if "all mail" in mbox.lower():
            candidates.append(mbox)

    for prefer in ["[Gmail]/All Mail", "[Google Mail]/All Mail", "All Mail"]:
        for c in candidates:
            if c == prefer:
                return c

    return candidates[0] if candidates else None


def gmail_add_labels(gmail_conn, uid, labels, folder=None):
    clean = []
    seen = set()
    for x in labels or []:
        if not x:
            continue
        x = str(x).strip()
        if not x:
            continue
        if x not in seen:
            seen.add(x)
            clean.append(x)

    if not clean:
        return True

    if folder:
        gmail_select_folder(gmail_conn, folder)
    else:
        # Best-effort fallback: select All Mail if known, otherwise INBOX.
        sel = getattr(gmail_conn, "_all_mail_folder", None) or "INBOX"
        gmail_select_folder(gmail_conn, sel)

    def _fmt_label(label):
        # Gmail IMAP expects system labels like \Inbox as atoms (unquoted).
        if label.startswith("\\"):
            return label
        # Quote and escape for IMAP quoted string.
        safe = label.replace("\\", "\\\\").replace('"', "")
        return '"%s"' % safe

    label_list = "(" + " ".join([_fmt_label(l) for l in clean]) + ")"
    typ, data = gmail_conn.uid("STORE", str(uid), "+X-GM-LABELS.SILENT", label_list)
    if typ != "OK":
        raise RuntimeError("Gmail X-GM-LABELS STORE failed for uid %s: %s" % (uid, data))
    return True


def parse_dest_labels(dest_folder_value, default_label):
    # dest_folder can be a comma-separated list of labels.
    # If empty, fall back to the default label (source email address).
    raw = (dest_folder_value or "").strip()
    if not raw:
        return [default_label] if default_label else []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def gmail_append(gmail_conn, raw_msg, flags, append_date_value, labels):
    # Append into All Mail when possible, otherwise INBOX. Then apply provided labels.
    keep = []

    try:
        internaldate = imaplib.Time2Internaldate(append_date_value)
    except Exception as e:
        logging.warning("Could not convert append date (%s); appending with server time", e)
        internaldate = None

    if not hasattr(gmail_conn, "_all_mail_folder"):
        gmail_conn._all_mail_folder = gmail_find_all_mail_folder(gmail_conn)

    append_folder = gmail_conn._all_mail_folder or "INBOX"

    typ, data = gmail_conn.append('"%s"' % append_folder, " ".join(keep), internaldate, raw_msg)
    if typ != "OK":
        raise RuntimeError("Gmail APPEND failed (folder=%s): %s" % (append_folder, data))

    au = _parse_appenduid(data)
    if au is None:
        logging.warning("APPEND ok but no APPENDUID returned; cannot apply X-GM-LABELS for this message")
        return True

    _uidvalidity, new_uid = au
    gmail_add_labels(gmail_conn, new_uid, labels, folder=append_folder)
    logging.debug("Gmail APPEND ok folder=%s uid=%s labels=%s", append_folder, new_uid, labels)
    return True


def source_delete_uid(source_conn, uid):
    # Mark message deleted on source; actual delete happens on EXPUNGE.
    logging.debug("Marking source UID %s as deleted", uid)
    typ, data = source_conn.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
    if typ != "OK":
        raise RuntimeError("STORE +Deleted failed for uid %s: %s" % (uid, data))


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


def main():
    if len(sys.argv) < 2:
        print("Usage: mailfetcher.py /path/to/config.ini")
        return 2

    # Load config and general settings.
    cp = load_config(sys.argv[1])

    poll_seconds = int(cp.get("general", "poll_seconds", fallback="60"))
    state_db_path = cp.get("general", "state_db", fallback="./state.sqlite3")
    log_level = cp.get("general", "log_level", fallback="INFO").upper()
    create_labels = cp.get("general", "create_labels", fallback="yes").lower() in ("1", "yes", "true", "on")
    imap_timeout_seconds = int(cp.get("general", "imap_timeout_seconds", fallback="60"))

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.info("Starting mailfetcher")
    logging.info(
        "Settings: poll_seconds=%s state_db=%s log_level=%s create_labels=%s imap_timeout_seconds=%s",
        poll_seconds,
        state_db_path,
        log_level,
        create_labels,
        imap_timeout_seconds,
    )

    # Initialize local sync state DB.
    db = StateDB(state_db_path)

    # Build Gmail profiles from config.
    gmail_profiles = {}
    for sec in cp.sections():
        if sec.startswith("gmail_"):
            gmail_profiles[sec] = {
                "host": cp.get(sec, "host"),
                "port": int(cp.get(sec, "port", fallback="993")),
                "username": cp.get(sec, "username"),
                "app_password": get_secret_from_env(cp, sec, "app_password_env", fallback_plain_key="app_password"),
            }

    if not gmail_profiles:
        logging.error("No gmail_* profiles in config")
        return 2

    logging.info("Configured %d Gmail profile(s)", len(gmail_profiles))
    source_sections = list(iter_source_sections(cp))
    logging.info("Configured %d source mailbox section(s)", len(source_sections))

    while not STOP:
        loop_start = time.time()
        gmail_conns = {}
        try:
            logging.info("Starting poll cycle")
            # Connect to all Gmail profiles first (shared across sources).
            for name, g in gmail_profiles.items():
                gmail_conns[name] = imap_login(
                    g["host"], g["port"], g["username"], g["app_password"], timeout=imap_timeout_seconds
                )
                logging.info("Connected Gmail profile %s as %s", name, g["username"])

            # Process each source section sequentially.
            for sec in source_sections:
                if STOP:
                    break

                shost = cp.get(sec, "source_host")
                sport = int(cp.get(sec, "source_port", fallback="993"))
                suser = cp.get(sec, "source_username")
                spass = get_secret_from_env(cp, sec, "source_password_env", fallback_plain_key="source_password")
                sfolder = cp.get(sec, "source_folder", fallback="INBOX")
                dprofile = cp.get(sec, "dest_profile")

                # Default label is the source email address
                dfolder = cp.get(sec, "dest_folder", fallback="")

                if dprofile not in gmail_conns:
                    logging.error("[%s] dest_profile %s not found", sec, dprofile)
                    continue

                # mailbox_id uniquely identifies a source folder for state tracking.
                mailbox_id = "%s|%s|%s" % (shost, suser, sfolder)
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

                # Retry loop for transient network or IMAP issues.
                backoff = 1
                for attempt in range(1, 6):
                    try:
                        logging.info("[%s] Attempt %d/5: connecting source %s", sec, attempt, suser)
                        src = imap_login(shost, sport, suser, spass, timeout=imap_timeout_seconds)

                        try:
                            # Sync state check: UIDVALIDITY + last UID.
                            imap_select(src, sfolder)
                            uidvalidity = imap_get_uidvalidity(src, sfolder)
                            logging.info("[%s] UIDVALIDITY=%d", sec, uidvalidity)

                            st = db.get(mailbox_id)
                            if st is None:
                                db.put(mailbox_id, uidvalidity, 0)
                                st = {"uidvalidity": uidvalidity, "last_uid": 0}
                                logging.info("[%s] No prior state found; initialized last_uid=0", sec)

                            if st["uidvalidity"] != uidvalidity:
                                logging.warning(
                                    "[%s] UIDVALIDITY changed (%d -> %d). Resetting last_uid to 0",
                                    sec,
                                    st["uidvalidity"],
                                    uidvalidity,
                                )
                                db.put(mailbox_id, uidvalidity, 0)
                                st = {"uidvalidity": uidvalidity, "last_uid": 0}

                            last_uid = st["last_uid"]
                            logging.info("[%s] Last processed UID=%d", sec, last_uid)

                            uids = imap_search_uids(src, last_uid)
                            if not uids:
                                # If another service deleted messages, last_uid may be beyond any existing UID.
                                max_uid = imap_get_max_uid(src)
                                if max_uid is not None and last_uid > max_uid:
                                    logging.warning(
                                        "[%s] last_uid=%d exceeds max_uid=%d; resyncing state",
                                        sec,
                                        last_uid,
                                        max_uid,
                                    )
                                    db.put(mailbox_id, uidvalidity, max_uid)
                                    last_uid = max_uid
                                    uids = imap_search_uids(src, last_uid)

                            if not uids:
                                logging.info("[%s] No new messages", sec)
                                src.logout()
                                break

                            uids.sort()
                            logging.info("[%s] Found %d message(s), UID range %d..%d", sec, len(uids), uids[0], uids[-1])

                            gmail = gmail_conns[dprofile]

                            dest_labels = parse_dest_labels(dfolder, suser)

                            if create_labels and dest_labels:
                                for lbl in dest_labels:
                                    # Skip creating system labels like \Inbox.
                                    if lbl.startswith("\\"):
                                        continue
                                    created = imap_create_folder_if_needed(gmail, lbl)
                                    logging.info(
                                        "[%s] Destination label %s ready (created_or_exists=%s)",
                                        sec,
                                        lbl,
                                        created,
                                    )

                            moved = 0
                            for uid in uids:
                                if STOP:
                                    break

                                # Fetch source message and append to Gmail.
                                logging.info("[%s] Copying UID %d", sec, uid)
                                raw_msg, flags, internaldate_str, header_date_str = imap_fetch_rfc822(src, uid)
                                append_dt = choose_append_date(internaldate_str, header_date_str)

                                # Apply configured labels (defaults to source email address)
                                gmail_append(gmail, raw_msg, flags, append_dt, dest_labels)

                                source_delete_uid(src, uid)
                                moved += 1

                                # Persist progress so restarts don't re-copy mail.
                                db.put(mailbox_id, uidvalidity, uid)
                                logging.info("[%s] UID %d copied and checkpoint saved", sec, uid)

                            if moved > 0:
                                # Permanently remove from source after all appends succeed.
                                source_expunge(src)
                                logging.info(
                                    "[%s] Moved %d messages to %s (labels: %s)",
                                    sec,
                                    moved,
                                    dprofile,
                                    ", ".join(dest_labels) if dest_labels else "(none)",
                                )

                            src.logout()
                            break

                        except Exception:
                            # Ensure source connection is closed on failures.
                            try:
                                src.logout()
                            except Exception:
                                pass
                            raise

                    except Exception as e:
                        logging.warning("[%s] Attempt %d failed: %s", sec, attempt, str(e))
                        logging.info("[%s] Sleeping %ds before retry", sec, backoff)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                else:
                    logging.error("[%s] Exhausted retries after 5 attempts", sec)

            # Always close Gmail connections for this loop.
            for c in gmail_conns.values():
                try:
                    c.logout()
                except Exception:
                    pass
            logging.info("Poll cycle complete")

        except Exception as e:
            logging.error("Loop error: %s", str(e))
            for c in gmail_conns.values():
                try:
                    c.logout()
                except Exception:
                    pass

        # Sleep until the next poll; allow SIGINT/SIGTERM to interrupt.
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
