#!/usr/bin/env python3
"""
Mail fetcher:
- Connects to one or more source IMAP mailboxes.
- Appends new messages into Gmail via IMAP APPEND.
- Deletes from source only after a successful append.
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
    import socket

    socket.setdefaulttimeout(timeout)
    ctx = ssl.create_default_context()
    c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    typ, data = c.login(username, password)
    if typ != "OK":
        raise RuntimeError("IMAP login failed for %s: %s" % (username, data))
    return c


def imap_select(c, folder):
    # Select folder (read/write) for UID and FETCH operations.
    typ, data = c.select('"%s"' % folder, readonly=False)
    if typ != "OK":
        raise RuntimeError("SELECT failed for folder %s: %s" % (folder, data))
    return data


def imap_get_uidvalidity(c):
    # UIDVALIDITY changes mean UIDs can no longer be trusted (RFC 3501).
    typ, data = c.response("UIDVALIDITY")
    if typ != "OK" or not data or not data[0]:
        raise RuntimeError("Could not read UIDVALIDITY")
    return int(data[0])


def imap_create_folder_if_needed(c, folder):
    # Create a Gmail label (folder) if configured; no-op if it already exists.
    typ, _ = c.create('"%s"' % folder)
    if typ == "OK":
        return True
    typ2, data2 = c.list(directory='""', pattern='"%s"' % folder)
    if typ2 == "OK" and data2:
        return True
    return False


def imap_search_uids(c, last_uid):
    # Search for new UIDs strictly greater than last_uid.
    q = "UID %d:*" % (last_uid + 1)
    typ, data = c.uid("SEARCH", None, q)
    if typ != "OK":
        raise RuntimeError("UID SEARCH failed: %s" % (data,))
    if not data or not data[0]:
        return []
    parts = data[0].split()
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    return out


def imap_fetch_rfc822(c, uid):
    # Fetch full message, flags, internal date and Date header.
    typ, data = c.uid(
        "FETCH",
        str(uid),
        "(RFC822 FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE)])",
    )
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
            if blob:
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

        elif isinstance(item, bytes):
            s = item.decode("utf-8", errors="ignore")
            for line in s.splitlines():
                if line.lower().startswith("date:"):
                    header_date = line[5:].strip()
                    break

    if raw_msg is None:
        raise RuntimeError("RFC822 body missing for uid %s" % uid)

    return raw_msg, flags, internaldate, header_date


def parse_imap_internaldate_to_tuple(internaldate_str):
    # Parse IMAP INTERNALDATE into a time tuple for APPEND.
    try:
        import datetime
        import re

        m = re.match(
            r"(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})",
            internaldate_str,
        )
        if not m:
            return time.localtime()

        day = int(m.group(1))
        mon_s = m.group(2).lower()
        year = int(m.group(3))
        hh = int(m.group(4))
        mm = int(m.group(5))
        ss = int(m.group(6))

        mons = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        mon = mons.get(mon_s, 1)

        dt = datetime.datetime(year, mon, day, hh, mm, ss)
        return dt.timetuple()
    except Exception:
        return time.localtime()


def choose_append_date(internaldate_str, header_date_str):
    # Prefer INTERNALDATE; fall back to Date header or now.
    if internaldate_str:
        return parse_imap_internaldate_to_tuple(internaldate_str)
    if header_date_str:
        try:
            dt = parsedate_to_datetime(header_date_str)
            return dt.timetuple()
        except Exception:
            pass
    return time.localtime()


def gmail_append(gmail_conn, folder, raw_msg, flags, append_date_tuple):
    # Preserve Seen flag and message timestamp when appending.
    keep = []
    if "\\Seen" in flags:
        keep.append("\\Seen")
    internaldate = imaplib.Time2Internaldate(append_date_tuple)

    typ, data = gmail_conn.append('"%s"' % folder, " ".join(keep), internaldate, raw_msg)
    if typ != "OK":
        raise RuntimeError("Gmail APPEND failed: %s" % (data,))
    return True


def source_delete_uid(source_conn, uid):
    # Mark message deleted on source; actual delete happens on EXPUNGE.
    typ, data = source_conn.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
    if typ != "OK":
        raise RuntimeError("STORE +Deleted failed for uid %s: %s" % (uid, data))


def source_expunge(source_conn):
    # Permanently remove messages marked as \Deleted.
    typ, data = source_conn.expunge()
    if typ != "OK":
        raise RuntimeError("EXPUNGE failed: %s" % (data,))


def load_config(path):
    # Load INI config, keeping section names as-is.
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
    create_labels = cp.get("general", "create_labels", fallback="yes").lower() in (
        "1",
        "yes",
        "true",
        "on",
    )

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
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

    while not STOP:
        loop_start = time.time()
        gmail_conns = {}
        try:
            # Connect to all Gmail profiles first (shared across sources).
            for name, g in gmail_profiles.items():
                gmail_conns[name] = imap_login(g["host"], g["port"], g["username"], g["app_password"])
                logging.info("Connected Gmail profile %s as %s", name, g["username"])

            # Process each source section sequentially.
            for sec in iter_source_sections(cp):
                if STOP:
                    break

                shost = cp.get(sec, "source_host")
                sport = int(cp.get(sec, "source_port", fallback="993"))
                suser = cp.get(sec, "source_username")
                spass = get_secret_from_env(cp, sec, "source_password_env", fallback_plain_key="source_password")
                sfolder = cp.get(sec, "source_folder", fallback="INBOX")
                dprofile = cp.get(sec, "dest_profile")
                dfolder = cp.get(sec, "dest_folder")

                if dprofile not in gmail_conns:
                    logging.error("[%s] dest_profile %s not found", sec, dprofile)
                    continue

                # mailbox_id uniquely identifies a source folder for state tracking.
                mailbox_id = "%s|%s|%s" % (shost, suser, sfolder)

                # Retry loop for transient network or IMAP issues.
                backoff = 1
                for attempt in range(1, 6):
                    try:
                        logging.info("[%s] Connecting source %s", sec, suser)
                        src = imap_login(shost, sport, suser, spass)

                        try:
                            # Sync state check: UIDVALIDITY + last UID.
                            imap_select(src, sfolder)
                            uidvalidity = imap_get_uidvalidity(src)

                            st = db.get(mailbox_id)
                            if st is None:
                                db.put(mailbox_id, uidvalidity, 0)
                                st = {"uidvalidity": uidvalidity, "last_uid": 0}

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
                            uids = imap_search_uids(src, last_uid)
                            if not uids:
                                logging.info("[%s] No new messages", sec)
                                src.logout()
                                break

                            uids.sort()
                            gmail = gmail_conns[dprofile]

                            # Create Gmail label if requested.
                            if create_labels:
                                imap_create_folder_if_needed(gmail, dfolder)

                            moved = 0
                            for uid in uids:
                                if STOP:
                                    break

                                # Fetch source message and append to Gmail.
                                raw_msg, flags, internaldate_str, header_date_str = imap_fetch_rfc822(src, uid)
                                append_dt = choose_append_date(internaldate_str, header_date_str)

                                gmail_append(gmail, dfolder, raw_msg, flags, append_dt)

                                # Mark source for deletion after successful append.
                                source_delete_uid(src, uid)
                                moved += 1

                                # Persist progress so restarts don't re-copy mail.
                                db.put(mailbox_id, uidvalidity, uid)

                            if moved > 0:
                                # Permanently remove from source after all appends succeed.
                                source_expunge(src)
                                logging.info("[%s] Moved %d messages to %s/%s", sec, moved, dprofile, dfolder)

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
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 60)

            # Always close Gmail connections for this loop.
            for c in gmail_conns.values():
                try:
                    c.logout()
                except Exception:
                    pass

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
        for _ in range(sleep_s):
            if STOP:
                break
            time.sleep(1)

    logging.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
