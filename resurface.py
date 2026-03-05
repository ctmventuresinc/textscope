#!/usr/bin/env python3
"""
Resurface — Pull things back up from your iMessages.
A local web app to search texts, find links, and surface email addresses.
"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

IMESSAGE_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")
PORT = 5050

URL_REGEX = r'https?://[^\s<>"\')\]]*'
EMAIL_REGEX = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'


def check_access():
    if not os.path.exists(IMESSAGE_DB):
        print("\n[FATAL] Not macOS (no chat.db found).")
        sys.exit(1)
    try:
        conn = sqlite3.connect(IMESSAGE_DB)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        conn.close()
    except Exception:
        print("\n⚠️  ACCESS DENIED")
        print("   System Settings → Privacy & Security → Full Disk Access → Add Terminal")
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"]
        )
        sys.exit(1)


def db_query(sql, params=()):
    conn = sqlite3.connect(IMESSAGE_DB)
    r = conn.execute(sql, params).fetchall()
    conn.close()
    return r


def extract_contacts():
    contacts = {}
    db_paths = glob.glob(
        os.path.join(ADDRESSBOOK_DIR, "Sources", "*", "AddressBook-v22.abcddb")
    )
    main_db = os.path.join(ADDRESSBOOK_DIR, "AddressBook-v22.abcddb")
    if os.path.exists(main_db):
        db_paths.append(main_db)

    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            people = {}
            for row in conn.execute(
                "SELECT ROWID, ZFIRSTNAME, ZLASTNAME FROM ZABCDRECORD "
                "WHERE ZFIRSTNAME IS NOT NULL OR ZLASTNAME IS NOT NULL"
            ):
                name = f"{row[1] or ''} {row[2] or ''}".strip()
                if name:
                    people[row[0]] = name

            for owner, phone in conn.execute(
                "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
            ):
                if owner in people:
                    name = people[owner]
                    digits = re.sub(r"\D", "", str(phone))
                    if digits:
                        contacts[digits] = name
                        if len(digits) >= 10:
                            contacts[digits[-10:]] = name
                        if len(digits) >= 7:
                            contacts[digits[-7:]] = name
                        if len(digits) == 11 and digits.startswith("1"):
                            contacts[digits[1:]] = name

            for owner, email in conn.execute(
                "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
            ):
                if owner in people:
                    contacts[email.lower().strip()] = people[owner]

            conn.close()
        except Exception:
            pass

    return contacts


def normalize_phone(phone):
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else (digits if digits else None)


def get_name(handle, contacts):
    if not handle:
        return ""
    if "@" in handle:
        lookup = handle.lower().strip()
        if lookup in contacts:
            return contacts[lookup]
        return handle.split("@")[0]
    digits = re.sub(r"\D", "", str(handle))
    if digits in contacts:
        return contacts[digits]
    if len(digits) == 11 and digits.startswith("1") and digits[1:] in contacts:
        return contacts[digits[1:]]
    if len(digits) >= 10 and digits[-10:] in contacts:
        return contacts[digits[-10:]]
    if len(digits) >= 7 and digits[-7:] in contacts:
        return contacts[digits[-7:]]
    return str(handle)


def get_all_handles():
    rows = db_query("""
        SELECT h.id, COUNT(m.ROWID) as msg_count
        FROM handle h
        JOIN message m ON m.handle_id = h.ROWID
        GROUP BY h.id
        ORDER BY msg_count DESC
    """)
    return rows


def extract_msg_text(text, attr_body):
    """Get message text, falling back to attributedBody."""
    msg_text = text or ""
    if not msg_text and attr_body:
        try:
            part = attr_body.split(b"NSString")[1][5:]
            idx = part.find(b"NSDictionary")
            if idx > 0:
                part = part[:idx]
            msg_text = re.sub(r'[^\x20-\x7e]+', '', part.decode("utf-8", errors="replace")).strip()
        except Exception:
            pass
    return msg_text


def extract_attr_urls(attr_body):
    """Extract clean URLs from attributedBody binary."""
    if not attr_body:
        return []
    urls = []
    raw = re.findall(rb'https?://[a-zA-Z0-9_.~:/?#\[\]@!$&\'()*+,;=%-]+', attr_body)
    for u in raw:
        decoded = u.decode("ascii", errors="ignore").strip()
        if decoded and len(decoded) > 8:
            urls.append(decoded)
    return urls


def dedup_urls(urls):
    """Deduplicate URLs — if one is a prefix of another, keep shorter."""
    combined = sorted(set(urls), key=len)
    result = []
    for u in combined:
        if not any(u.startswith(existing) and u != existing for existing in result):
            result = [e for e in result if not e.startswith(u)]
            result.append(u)
    return result


def search_messages(handles, keywords):
    """Search messages from given handles for URLs/text matching keywords."""
    url_re = re.compile(URL_REGEX)

    handle_conditions = []
    params = []
    for h in handles:
        if "@" in h:
            handle_conditions.append("LOWER(h.id) = ?")
            params.append(h.lower())
        else:
            norm = normalize_phone(h)
            if norm:
                handle_conditions.append("h.id LIKE ?")
                params.append(f"%{norm[-10:]}")

    if not handle_conditions:
        return {"results": [], "total_searched": 0}

    where_handles = " OR ".join(handle_conditions)
    sql = f"""
        SELECT
            (m.date/1000000000+978307200) AS ts,
            h.id AS handle,
            m.text,
            m.is_from_me,
            c.display_name AS chat_name,
            m.attributedBody
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE ({where_handles})
          AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
        ORDER BY (m.date/1000000000+978307200) DESC
    """
    rows = db_query(sql, tuple(params))

    contacts = extract_contacts()
    results = []
    total_searched = len(rows)

    for ts, handle, text, is_from_me, chat_name, attr_body in rows:
        msg_text = extract_msg_text(text, attr_body)
        extra_urls = extract_attr_urls(attr_body)
        urls = url_re.findall(msg_text)
        all_urls = dedup_urls(urls + extra_urls)

        kw_lower = [k.lower().strip() for k in keywords if k.strip()]
        if kw_lower:
            matched_urls = [u for u in all_urls if any(kw in u.lower() for kw in kw_lower)]
            text_matches = any(kw in msg_text.lower() for kw in kw_lower) if msg_text else False
            if not matched_urls and not text_matches:
                continue
        else:
            matched_urls = all_urls
            if not matched_urls:
                continue

        if not msg_text:
            msg_text = " ".join(matched_urls) if matched_urls else "(attachment)"

        sender_name = get_name(handle, contacts)
        try:
            dt_str = datetime.fromtimestamp(int(ts)).strftime("%b %d, %Y  %I:%M %p")
        except Exception:
            dt_str = "Unknown"

        results.append({
            "dt": dt_str,
            "from_me": bool(is_from_me),
            "sender": sender_name,
            "handle": handle,
            "matched_urls": matched_urls,
            "text": msg_text[:1000],
        })

    return {"results": results, "total_searched": total_searched}


def find_emails(days=365):
    """Find all email addresses in iMessages from the last N days."""
    cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp()) - 978307200
    cutoff_apple = cutoff_ts * 1000000000

    email_re = re.compile(EMAIL_REGEX)

    sql = """
        SELECT
            (m.date/1000000000+978307200) AS ts,
            h.id AS handle,
            m.text,
            m.is_from_me,
            m.attributedBody
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.date >= ?
          AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
        ORDER BY (m.date/1000000000+978307200) DESC
    """
    rows = db_query(sql, (cutoff_apple,))

    contacts = extract_contacts()
    results = []
    seen_emails = {}
    total_searched = len(rows)

    # Known iMessage handle emails to skip (they're not "sent" emails)
    skip_domains = {"imessage.apple.com"}

    for ts, handle, text, is_from_me, attr_body in rows:
        msg_text = extract_msg_text(text, attr_body)

        # Also check attributedBody raw bytes for emails
        extra_text = ""
        if attr_body:
            extra_text = attr_body.decode("ascii", errors="ignore")

        combined_text = f"{msg_text} {extra_text}"
        found_emails = email_re.findall(combined_text)
        if not found_emails:
            continue

        sender_name = get_name(handle, contacts)
        try:
            dt_str = datetime.fromtimestamp(int(ts)).strftime("%b %d, %Y  %I:%M %p")
        except Exception:
            dt_str = "Unknown"

        for email in found_emails:
            email_lower = email.lower()
            domain = email_lower.split("@")[1] if "@" in email_lower else ""

            # Skip iMessage system emails, image extensions, common false positives
            if domain in skip_domains:
                continue
            if re.search(r'\.(png|jpg|jpeg|gif|svg|webp|heic|mov|mp4|pdf)$', email_lower):
                continue
            # Skip emails with trailing junk from binary parsing
            # TLD must end cleanly (2-4 letter TLD like .com, .org, .io, .info)
            if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,4}$', email_lower):
                continue

            if email_lower not in seen_emails:
                seen_emails[email_lower] = {
                    "email": email_lower,
                    "first_seen": dt_str,
                    "last_seen": dt_str,
                    "count": 0,
                    "senders": set(),
                    "sample_text": "",
                }

            entry = seen_emails[email_lower]
            entry["count"] += 1
            entry["first_seen"] = dt_str  # since we're DESC, this overwrites to the oldest
            if sender_name:
                entry["senders"].add(sender_name)
            if not entry["sample_text"] and msg_text:
                entry["sample_text"] = msg_text[:300]

    # Convert to list sorted by most recent
    for e in seen_emails.values():
        e["senders"] = list(e["senders"])[:5]
        results.append(e)

    return {"results": results, "total_searched": total_searched}


# ─── HTML ────────────────────────────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Resurface</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    :root {
      --glass-bg: rgba(255,255,255,0.55);
      --glass-border: rgba(255,255,255,0.7);
      --glass-shadow: 0 8px 32px rgba(0,120,200,0.12);
      --accent: #0091EA;
      --accent2: #00BFA5;
      --text: #1a2a3a;
      --text-muted: #5a7a8a;
      --radius: 16px;
    }

    * { margin:0; padding:0; box-sizing:border-box; }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      min-height: 100vh;
      background:
        radial-gradient(ellipse at 20% 50%, rgba(0,180,220,0.25) 0%, transparent 50%),
        radial-gradient(ellipse at 80% 20%, rgba(0,220,160,0.2) 0%, transparent 50%),
        radial-gradient(ellipse at 50% 80%, rgba(100,180,255,0.15) 0%, transparent 50%),
        linear-gradient(180deg, #d4f1ff 0%, #e8faf3 40%, #c9e8ff 100%);
      color: var(--text);
    }

    .app {
      max-width: 960px;
      margin: 0 auto;
      padding: 40px 20px 80px;
    }

    .header {
      text-align: center;
      margin-bottom: 36px;
    }
    .header h1 {
      font-size: 42px;
      font-weight: 700;
      background: linear-gradient(135deg, #0077cc, #00bfa5);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -1px;
      margin-bottom: 6px;
    }
    .header p {
      font-size: 15px;
      color: var(--text-muted);
      font-weight: 400;
    }

    /* ─── Tabs ─── */
    .tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 24px;
      background: rgba(255,255,255,0.3);
      border-radius: 14px;
      padding: 4px;
    }
    .tab {
      flex: 1;
      padding: 12px;
      border: none;
      border-radius: 12px;
      font-size: 14px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      background: transparent;
      color: var(--text-muted);
      transition: all 0.2s;
    }
    .tab.active {
      background: var(--glass-bg);
      backdrop-filter: blur(16px);
      color: var(--text);
      box-shadow: 0 2px 12px rgba(0,80,150,0.1);
    }
    .tab:hover:not(.active) { background: rgba(255,255,255,0.3); }

    /* ─── Glass Card ─── */
    .glass {
      background: var(--glass-bg);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius);
      box-shadow: var(--glass-shadow);
      padding: 28px;
      margin-bottom: 24px;
    }

    .field-group { display: flex; flex-direction: column; gap: 6px; margin-bottom: 18px; }
    .field-group:last-child { margin-bottom: 0; }
    .field-group label {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
    }

    .contact-picker { position: relative; }
    input[type="text"], select {
      width: 100%;
      padding: 14px 16px;
      border: 1px solid rgba(0,120,200,0.2);
      border-radius: 12px;
      font-size: 15px;
      font-family: inherit;
      background: rgba(255,255,255,0.7);
      outline: none;
      transition: border 0.2s, box-shadow 0.2s;
      color: var(--text);
    }
    input[type="text"]:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(0,145,234,0.15);
    }

    .dropdown {
      position: absolute;
      top: 100%;
      left: 0; right: 0;
      max-height: 280px;
      overflow-y: auto;
      background: rgba(255,255,255,0.95);
      backdrop-filter: blur(16px);
      border: 1px solid rgba(0,120,200,0.15);
      border-radius: 12px;
      margin-top: 4px;
      box-shadow: 0 12px 40px rgba(0,80,150,0.15);
      z-index: 100;
      display: none;
    }
    .dropdown.open { display: block; }
    .dropdown-item {
      padding: 12px 16px;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 14px;
      transition: background 0.15s;
    }
    .dropdown-item:hover { background: rgba(0,145,234,0.08); }
    .dropdown-item .name { font-weight: 500; }
    .dropdown-item .handle { font-size: 12px; color: var(--text-muted); margin-left: 8px; }
    .dropdown-item .count {
      font-size: 11px;
      background: rgba(0,145,234,0.1);
      color: var(--accent);
      padding: 2px 8px;
      border-radius: 20px;
      font-weight: 600;
      white-space: nowrap;
    }

    .selected-contacts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      background: linear-gradient(135deg, rgba(0,145,234,0.12), rgba(0,191,165,0.12));
      border: 1px solid rgba(0,145,234,0.2);
      border-radius: 20px;
      font-size: 13px;
      font-weight: 500;
    }
    .chip .remove {
      cursor: pointer;
      width: 18px; height: 18px;
      display: flex; align-items: center; justify-content: center;
      border-radius: 50%;
      background: rgba(0,0,0,0.1);
      font-size: 11px;
      transition: background 0.15s;
    }
    .chip .remove:hover { background: rgba(220,50,50,0.2); }

    .btn {
      width: 100%;
      padding: 14px 32px;
      border: none;
      border-radius: 12px;
      font-size: 15px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: transform 0.15s, box-shadow 0.15s;
      background: linear-gradient(135deg, #0091EA, #00BFA5);
      color: #fff;
      box-shadow: 0 4px 16px rgba(0,145,234,0.3);
      margin-top: 18px;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 24px rgba(0,145,234,0.4);
    }
    .btn:active { transform: translateY(0); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

    /* ─── Stats ─── */
    .stats {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      margin-bottom: 20px;
    }
    .stat {
      padding: 14px 20px;
      background: var(--glass-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--glass-border);
      border-radius: 12px;
      flex: 1;
      min-width: 140px;
      text-align: center;
    }
    .stat .num {
      font-size: 28px;
      font-weight: 700;
      background: linear-gradient(135deg, #0077cc, #00bfa5);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .stat .label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-top: 2px;
    }

    /* ─── Result Cards ─── */
    .result-card {
      background: var(--glass-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--glass-border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 12px;
      transition: transform 0.15s, box-shadow 0.15s;
    }
    .result-card:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 32px rgba(0,120,200,0.15);
    }
    .result-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 8px;
    }
    .result-meta .date { font-size: 13px; color: var(--text-muted); font-weight: 500; }
    .result-meta .direction {
      font-size: 12px; font-weight: 600;
      padding: 4px 10px; border-radius: 20px;
    }
    .direction.sent { background: rgba(0,145,234,0.1); color: #0077cc; }
    .direction.received { background: rgba(0,191,165,0.1); color: #00897B; }

    .result-links {
      display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;
    }
    .result-links a {
      display: inline-block;
      padding: 10px 14px;
      background: linear-gradient(135deg, #0091EA, #00BFA5);
      color: #fff;
      text-decoration: none;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 500;
      word-break: break-all;
      transition: opacity 0.15s;
    }
    .result-links a:hover { opacity: 0.85; }

    .result-text {
      font-size: 14px; line-height: 1.6; color: var(--text);
      padding: 14px;
      background: rgba(255,255,255,0.5);
      border-radius: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 200px;
      overflow-y: auto;
    }

    /* ─── Email Cards ─── */
    .email-card {
      background: var(--glass-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--glass-border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 10px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: center;
      transition: transform 0.15s, box-shadow 0.15s;
    }
    .email-card:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 32px rgba(0,120,200,0.15);
    }
    .email-addr {
      font-size: 16px;
      font-weight: 600;
      color: var(--text);
      word-break: break-all;
    }
    .email-detail {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 4px;
      line-height: 1.5;
    }
    .email-count {
      font-size: 13px;
      font-weight: 700;
      background: linear-gradient(135deg, rgba(0,145,234,0.1), rgba(0,191,165,0.1));
      color: var(--accent);
      padding: 6px 14px;
      border-radius: 20px;
      white-space: nowrap;
    }
    .email-context {
      grid-column: 1 / -1;
      font-size: 13px;
      color: var(--text-muted);
      padding: 10px 14px;
      background: rgba(255,255,255,0.4);
      border-radius: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 80px;
      overflow: hidden;
    }
    .copy-btn {
      background: none;
      border: 1px solid rgba(0,120,200,0.2);
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 12px;
      color: var(--text-muted);
      transition: all 0.15s;
      font-family: inherit;
    }
    .copy-btn:hover {
      background: rgba(0,145,234,0.08);
      border-color: var(--accent);
      color: var(--accent);
    }

    .loading {
      text-align: center; padding: 40px; color: var(--text-muted);
    }
    .loading .spinner {
      width: 36px; height: 36px;
      border: 3px solid rgba(0,145,234,0.15);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 12px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .empty-state {
      text-align: center; padding: 60px 20px; color: var(--text-muted);
    }
    .empty-state .icon { font-size: 48px; margin-bottom: 12px; }

    .hidden { display: none; }

    @media (max-width: 600px) {
      .header h1 { font-size: 30px; }
      .glass { padding: 20px; }
      .stats { flex-direction: column; }
      .email-card { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="header">
      <h1>🫧 Resurface</h1>
      <p>Pull things back up from your iMessages</p>
    </div>

    <div class="tabs">
      <button class="tab active" data-tab="emails" onclick="switchTab('emails')">📧 Email Finder</button>
      <button class="tab" data-tab="search" onclick="switchTab('search')">🔍 Keyword Search</button>
    </div>

    <!-- Email Finder Tab -->
    <div id="tab-emails">
      <div class="glass">
        <div class="field-group">
          <label>Time Range</label>
          <select id="emailRange">
            <option value="30">Last 30 days</option>
            <option value="90">Last 3 months</option>
            <option value="180">Last 6 months</option>
            <option value="365" selected>Last year</option>
            <option value="730">Last 2 years</option>
            <option value="9999">All time</option>
          </select>
        </div>
        <button class="btn" id="emailBtn" onclick="findEmails()">Find Emails</button>
      </div>
      <div id="emailResults" class="hidden"></div>
    </div>

    <!-- Keyword Search Tab -->
    <div id="tab-search" class="hidden">
      <div class="glass">
        <div class="field-group">
          <label>Contact</label>
          <div class="contact-picker">
            <input type="text" id="contactInput" placeholder="Start typing a name or number..." autocomplete="off" />
            <div class="dropdown" id="contactDropdown"></div>
          </div>
          <div class="selected-contacts" id="selectedContacts"></div>
        </div>
        <div class="field-group">
          <label>Keywords (comma separated)</label>
          <input type="text" id="keywordInput" placeholder='e.g. cancel, unsubscribe' />
        </div>
        <button class="btn" id="searchBtn" disabled>Search</button>
      </div>
      <div id="searchResults" class="hidden"></div>
    </div>
  </div>

  <script>
    let allHandles = [];
    let contacts = {};
    let selectedHandles = [];

    async function init() {
      const res = await fetch('/api/handles');
      const data = await res.json();
      allHandles = data.handles;
      contacts = data.contacts;
    }
    init();

    // ─── Tabs ───
    function switchTab(tab) {
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
      document.getElementById('tab-emails').classList.toggle('hidden', tab !== 'emails');
      document.getElementById('tab-search').classList.toggle('hidden', tab !== 'search');
    }

    // ─── Email Finder ───
    async function findEmails() {
      const days = document.getElementById('emailRange').value;
      const area = document.getElementById('emailResults');
      area.classList.remove('hidden');
      area.innerHTML = `<div class="loading"><div class="spinner"></div>Scanning messages for email addresses...</div>`;

      const res = await fetch('/api/emails', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: parseInt(days) })
      });
      const data = await res.json();
      renderEmails(data);
    }

    function renderEmails(data) {
      const area = document.getElementById('emailResults');
      const { results, total_searched } = data;

      if (!results.length) {
        area.innerHTML = `<div class="empty-state glass"><div class="icon">📭</div><p>No email addresses found in ${total_searched.toLocaleString()} messages.</p></div>`;
        return;
      }

      let html = `
        <div class="stats">
          <div class="stat"><div class="num">${total_searched.toLocaleString()}</div><div class="label">Messages Scanned</div></div>
          <div class="stat"><div class="num">${results.length.toLocaleString()}</div><div class="label">Emails Found</div></div>
        </div>`;

      for (const e of results) {
        const senders = e.senders.length ? e.senders.join(', ') : 'Unknown';
        html += `
          <div class="email-card">
            <div>
              <div class="email-addr">${esc(e.email)}</div>
              <div class="email-detail">
                From: ${esc(senders)} · Last seen: ${esc(e.last_seen)}
                ${e.count > 1 ? ` · First seen: ${esc(e.first_seen)}` : ''}
              </div>
            </div>
            <div style="display:flex;gap:8px;align-items:center">
              <span class="email-count">${e.count}×</span>
              <button class="copy-btn" onclick="copyEmail('${esc(e.email)}', this)">Copy</button>
            </div>
            ${e.sample_text ? `<div class="email-context">${esc(e.sample_text)}</div>` : ''}
          </div>`;
      }

      area.innerHTML = html;
    }

    function copyEmail(email, btn) {
      navigator.clipboard.writeText(email);
      btn.textContent = '✓';
      setTimeout(() => btn.textContent = 'Copy', 1500);
    }

    // ─── Keyword Search ───
    const contactInput = document.getElementById('contactInput');
    const dropdown = document.getElementById('contactDropdown');
    const selectedDiv = document.getElementById('selectedContacts');
    const searchBtn = document.getElementById('searchBtn');

    function renderDropdown(filter) {
      const lower = filter.toLowerCase();
      const matches = allHandles.filter(([h]) => {
        if (selectedHandles.includes(h)) return false;
        const name = (contacts[h] || '').toLowerCase();
        return h.toLowerCase().includes(lower) || name.includes(lower);
      }).slice(0, 20);

      if (!matches.length || !filter) { dropdown.classList.remove('open'); return; }

      dropdown.innerHTML = matches.map(([h, count]) => {
        const name = contacts[h];
        return `<div class="dropdown-item" data-handle="${esc(h)}">
          <span><span class="name">${esc(name || h)}</span>${name ? `<span class="handle">${esc(h)}</span>` : ''}</span>
          <span class="count">${count.toLocaleString()} msgs</span>
        </div>`;
      }).join('');
      dropdown.classList.add('open');
    }

    function addHandle(handle) {
      if (selectedHandles.includes(handle)) return;
      selectedHandles.push(handle);
      renderChips();
      contactInput.value = '';
      dropdown.classList.remove('open');
      searchBtn.disabled = false;
    }

    function removeHandle(handle) {
      selectedHandles = selectedHandles.filter(h => h !== handle);
      renderChips();
      searchBtn.disabled = selectedHandles.length === 0;
    }

    function renderChips() {
      selectedDiv.innerHTML = selectedHandles.map(h =>
        `<span class="chip">${esc(contacts[h] || h)} <span class="remove" onclick="removeHandle('${esc(h)}')">✕</span></span>`
      ).join('');
    }

    contactInput.addEventListener('input', () => renderDropdown(contactInput.value));
    contactInput.addEventListener('focus', () => { if (contactInput.value) renderDropdown(contactInput.value); });
    document.addEventListener('click', e => { if (!e.target.closest('.contact-picker')) dropdown.classList.remove('open'); });
    dropdown.addEventListener('click', e => { const item = e.target.closest('.dropdown-item'); if (item) addHandle(item.dataset.handle); });
    document.getElementById('keywordInput').addEventListener('keydown', e => { if (e.key === 'Enter') searchBtn.click(); });

    searchBtn.addEventListener('click', async () => {
      const keywords = document.getElementById('keywordInput').value;
      if (!selectedHandles.length) return;

      const area = document.getElementById('searchResults');
      area.classList.remove('hidden');
      area.innerHTML = `<div class="loading"><div class="spinner"></div>Searching messages...</div>`;

      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handles: selectedHandles, keywords })
      });
      const data = await res.json();
      renderSearchResults(data);
    });

    function renderSearchResults(data) {
      const area = document.getElementById('searchResults');
      const { results, total_searched } = data;

      if (!results.length) {
        area.innerHTML = `<div class="empty-state glass"><div class="icon">🔍</div><p>No matches in ${total_searched.toLocaleString()} messages.</p></div>`;
        return;
      }

      const totalLinks = results.reduce((s, r) => s + r.matched_urls.length, 0);
      let html = `
        <div class="stats">
          <div class="stat"><div class="num">${total_searched.toLocaleString()}</div><div class="label">Messages Searched</div></div>
          <div class="stat"><div class="num">${results.length.toLocaleString()}</div><div class="label">Matches</div></div>
          <div class="stat"><div class="num">${totalLinks.toLocaleString()}</div><div class="label">Links</div></div>
        </div>`;

      for (const r of results) {
        const dir = r.from_me ? 'sent' : 'received';
        const dirLabel = r.from_me ? 'You →' : `${esc(r.sender)} →`;
        const links = r.matched_urls.map(u => `<a href="${esc(u)}" target="_blank">🔗 ${esc(u)}</a>`).join('');
        html += `
          <div class="result-card">
            <div class="result-meta">
              <span class="date">${esc(r.dt)}</span>
              <span class="direction ${dir}">${dirLabel}</span>
            </div>
            ${links ? `<div class="result-links">${links}</div>` : ''}
            <div class="result-text">${esc(r.text)}</div>
          </div>`;
      }
      area.innerHTML = html;
    }

    function esc(s) {
      if (!s) return '';
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            self._respond(200, "text/html", INDEX_HTML.encode())
        elif path == "/api/handles":
            handles = get_all_handles()
            contact_map = {}
            c = extract_contacts()
            for h, count in handles:
                contact_map[h] = get_name(h, c)
            self._respond(200, "application/json", json.dumps({
                "handles": handles, "contacts": contact_map
            }).encode())
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))

        if path == "/api/search":
            handles = body.get("handles", [])
            keywords = [k.strip() for k in body.get("keywords", "").split(",") if k.strip()]
            result = search_messages(handles, keywords)
            self._respond(200, "application/json", json.dumps(result).encode())

        elif path == "/api/emails":
            days = body.get("days", 365)
            result = find_emails(days)
            self._respond(200, "application/json", json.dumps(result).encode())

        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(data)


def main():
    check_access()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n🫧 Resurface running at {url}")
    print("   Press Ctrl+C to stop\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Resurface stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
