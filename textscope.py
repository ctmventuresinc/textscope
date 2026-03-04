#!/usr/bin/env python3
"""
TextScope — Search through your iMessages.
A local web app to find links and keywords across your conversations.
"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

IMESSAGE_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")
PORT = 5050

URL_REGEX = r'https?://[^\s<>"\')\]]*'


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
    """Get all unique handles from the database with message counts."""
    rows = db_query("""
        SELECT h.id, COUNT(m.ROWID) as msg_count
        FROM handle h
        JOIN message m ON m.handle_id = h.ROWID
        GROUP BY h.id
        ORDER BY msg_count DESC
    """)
    return rows


def search_messages(handles, keywords):
    """Search messages from given handles for URLs matching keywords."""
    url_re = re.compile(URL_REGEX)

    # Normalize target phones and emails
    target_phones = set()
    target_emails = set()
    for h in handles:
        if "@" in h:
            target_emails.add(h.lower())
        else:
            norm = normalize_phone(h)
            if norm:
                target_phones.add(norm)

    # Build WHERE clause to filter by handle directly in SQL
    handle_conditions = []
    params = []
    for h in handles:
        if "@" in h:
            handle_conditions.append("LOWER(h.id) = ?")
            params.append(h.lower())
        else:
            # Match phone numbers with LIKE to handle +1 prefix etc
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

        # Extract text from attributedBody if text column is empty
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

        # Extract URLs from attributedBody
        extra_urls = []
        if attr_body:
            raw_urls = re.findall(rb'https?://[a-zA-Z0-9_.~:/?#\[\]@!$&\'()*+,;=%-]+', attr_body)
            for u in raw_urls:
                decoded = u.decode("ascii", errors="ignore").strip()
                if decoded and len(decoded) > 8:
                    extra_urls.append(decoded)

        # Find all URLs and deduplicate — if one URL starts with another, keep the shorter one
        urls = url_re.findall(msg_text)
        combined = sorted(set(urls + extra_urls), key=len)
        all_urls = []
        for u in combined:
            if not any(u.startswith(existing) and u != existing for existing in all_urls):
                # Remove any longer URLs that this one is a prefix of
                all_urls = [e for e in all_urls if not e.startswith(u)]
                all_urls.append(u)

        # Check keywords — search both URLs and message text
        kw_lower = [k.lower().strip() for k in keywords if k.strip()]
        if kw_lower:
            matched_urls = [u for u in all_urls if any(kw in u.lower() for kw in kw_lower)]
            text_matches = any(kw in msg_text.lower() for kw in kw_lower) if msg_text else False
            if not matched_urls and not text_matches:
                continue
        else:
            # No keywords — show all messages that have links
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
            "all_urls": all_urls,
            "text": msg_text[:1000],
            "chat": chat_name or "(1:1)",
        })

    return {"results": results, "total_searched": total_searched}


# ─── HTML ────────────────────────────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>TextScope</title>
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

    /* ─── Header ─── */
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

    /* ─── Search Form ─── */
    .search-form { display: flex; flex-direction: column; gap: 18px; }

    .field-group { display: flex; flex-direction: column; gap: 6px; }
    .field-group label {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
    }

    .contact-picker {
      position: relative;
    }
    .contact-input {
      width: 100%;
      padding: 14px 16px;
      border: 1px solid rgba(0,120,200,0.2);
      border-radius: 12px;
      font-size: 15px;
      font-family: inherit;
      background: rgba(255,255,255,0.7);
      outline: none;
      transition: border 0.2s, box-shadow 0.2s;
    }
    .contact-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(0,145,234,0.15);
    }

    .dropdown {
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
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
    .dropdown-item:hover, .dropdown-item.active {
      background: rgba(0,145,234,0.08);
    }
    .dropdown-item .name { font-weight: 500; }
    .dropdown-item .handle {
      font-size: 12px;
      color: var(--text-muted);
      margin-left: 8px;
    }
    .dropdown-item .count {
      font-size: 11px;
      background: rgba(0,145,234,0.1);
      color: var(--accent);
      padding: 2px 8px;
      border-radius: 20px;
      font-weight: 600;
      white-space: nowrap;
    }

    .selected-contacts {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 4px;
    }
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
      width: 18px;
      height: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 50%;
      background: rgba(0,0,0,0.1);
      font-size: 11px;
      transition: background 0.15s;
    }
    .chip .remove:hover { background: rgba(220,50,50,0.2); }

    .keyword-input {
      width: 100%;
      padding: 14px 16px;
      border: 1px solid rgba(0,120,200,0.2);
      border-radius: 12px;
      font-size: 15px;
      font-family: inherit;
      background: rgba(255,255,255,0.7);
      outline: none;
      transition: border 0.2s, box-shadow 0.2s;
    }
    .keyword-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(0,145,234,0.15);
    }

    .search-options {
      display: flex;
      gap: 12px;
      align-items: center;
    }
    .search-options label {
      font-size: 13px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      text-transform: none;
      letter-spacing: 0;
      color: var(--text);
      font-weight: 400;
    }

    .btn {
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
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 24px rgba(0,145,234,0.4);
    }
    .btn:active { transform: translateY(0); }
    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }

    /* ─── Results ─── */
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
    .result-meta .date {
      font-size: 13px;
      color: var(--text-muted);
      font-weight: 500;
    }
    .result-meta .direction {
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 20px;
    }
    .direction.sent {
      background: rgba(0,145,234,0.1);
      color: #0077cc;
    }
    .direction.received {
      background: rgba(0,191,165,0.1);
      color: #00897B;
    }

    .result-links {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-bottom: 12px;
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
      font-size: 14px;
      line-height: 1.6;
      color: var(--text);
      padding: 14px;
      background: rgba(255,255,255,0.5);
      border-radius: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 200px;
      overflow-y: auto;
    }

    .loading {
      text-align: center;
      padding: 40px;
      color: var(--text-muted);
    }
    .loading .spinner {
      width: 36px;
      height: 36px;
      border: 3px solid rgba(0,145,234,0.15);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 12px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .empty-state {
      text-align: center;
      padding: 60px 20px;
      color: var(--text-muted);
    }
    .empty-state .icon { font-size: 48px; margin-bottom: 12px; }

    .hidden { display: none; }

    @media (max-width: 600px) {
      .header h1 { font-size: 30px; }
      .glass { padding: 20px; }
      .stats { flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="header">
      <h1>🔭 TextScope</h1>
      <p>Search through your iMessages — find links, keywords, and more</p>
    </div>

    <div class="glass">
      <div class="search-form">
        <div class="field-group">
          <label>Contact</label>
          <div class="contact-picker">
            <input
              type="text"
              class="contact-input"
              id="contactInput"
              placeholder="Start typing a name or number..."
              autocomplete="off"
            />
            <div class="dropdown" id="contactDropdown"></div>
          </div>
          <div class="selected-contacts" id="selectedContacts"></div>
        </div>

        <div class="field-group">
          <label>Keywords (comma separated)</label>
          <input
            type="text"
            class="keyword-input"
            id="keywordInput"
            placeholder='e.g. cancel, unsubscribe'
          />
        </div>

        <div class="search-options">
          <label><input type="checkbox" id="urlOnly" checked /> Search in URLs</label>
          <label><input type="checkbox" id="textToo" /> Also search message text</label>
        </div>

        <button class="btn" id="searchBtn" disabled>Search</button>
      </div>
    </div>

    <div id="resultsArea" class="hidden"></div>
  </div>

  <script>
    let allHandles = [];
    let contacts = {};
    let selectedHandles = [];

    // Load contacts on start
    async function init() {
      const res = await fetch('/api/handles');
      const data = await res.json();
      allHandles = data.handles;
      contacts = data.contacts;
    }
    init();

    const contactInput = document.getElementById('contactInput');
    const dropdown = document.getElementById('contactDropdown');
    const selectedDiv = document.getElementById('selectedContacts');
    const keywordInput = document.getElementById('keywordInput');
    const searchBtn = document.getElementById('searchBtn');
    const resultsArea = document.getElementById('resultsArea');

    function getDisplayName(handle) {
      return contacts[handle] || handle;
    }

    function renderDropdown(filter) {
      const lower = filter.toLowerCase();
      const matches = allHandles.filter(([h, count]) => {
        if (selectedHandles.includes(h)) return false;
        const name = (contacts[h] || '').toLowerCase();
        return h.toLowerCase().includes(lower) || name.includes(lower);
      }).slice(0, 20);

      if (!matches.length || !filter) {
        dropdown.classList.remove('open');
        return;
      }

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
      updateBtn();
    }

    function removeHandle(handle) {
      selectedHandles = selectedHandles.filter(h => h !== handle);
      renderChips();
      updateBtn();
    }

    function renderChips() {
      selectedDiv.innerHTML = selectedHandles.map(h => {
        const name = contacts[h] || h;
        return `<span class="chip">${esc(name)} <span class="remove" onclick="removeHandle('${esc(h)}')">✕</span></span>`;
      }).join('');
    }

    function updateBtn() {
      searchBtn.disabled = selectedHandles.length === 0;
    }

    contactInput.addEventListener('input', () => renderDropdown(contactInput.value));
    contactInput.addEventListener('focus', () => { if (contactInput.value) renderDropdown(contactInput.value); });
    document.addEventListener('click', e => {
      if (!e.target.closest('.contact-picker')) dropdown.classList.remove('open');
    });
    dropdown.addEventListener('click', e => {
      const item = e.target.closest('.dropdown-item');
      if (item) addHandle(item.dataset.handle);
    });

    keywordInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') searchBtn.click();
    });

    searchBtn.addEventListener('click', async () => {
      const keywords = keywordInput.value;
      const urlOnly = document.getElementById('urlOnly').checked;
      const textToo = document.getElementById('textToo').checked;

      if (!selectedHandles.length) return;

      resultsArea.classList.remove('hidden');
      resultsArea.innerHTML = `<div class="loading"><div class="spinner"></div>Searching messages...</div>`;

      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          handles: selectedHandles,
          keywords: keywords,
          url_only: urlOnly,
          text_too: textToo
        })
      });
      const data = await res.json();
      renderResults(data);
    });

    function renderResults(data) {
      const { results, total_searched } = data;

      if (!results.length) {
        resultsArea.innerHTML = `
          <div class="empty-state glass">
            <div class="icon">🔍</div>
            <p>No matches found in ${total_searched.toLocaleString()} messages.</p>
          </div>`;
        return;
      }

      const totalLinks = results.reduce((s, r) => s + r.matched_urls.length, 0);

      let html = `
        <div class="stats">
          <div class="stat"><div class="num">${total_searched.toLocaleString()}</div><div class="label">Messages Searched</div></div>
          <div class="stat"><div class="num">${results.length.toLocaleString()}</div><div class="label">Matches Found</div></div>
          <div class="stat"><div class="num">${totalLinks.toLocaleString()}</div><div class="label">Links Found</div></div>
        </div>`;

      for (const r of results) {
        const dir = r.from_me ? 'sent' : 'received';
        const dirLabel = r.from_me ? 'You →' : `${esc(r.sender)} →`;
        const linksHtml = r.matched_urls.map(u =>
          `<a href="${esc(u)}" target="_blank">🔗 ${esc(u)}</a>`
        ).join('');

        html += `
          <div class="result-card">
            <div class="result-meta">
              <span class="date">${esc(r.dt)}</span>
              <span class="direction ${dir}">${dirLabel}</span>
            </div>
            ${linksHtml ? `<div class="result-links">${linksHtml}</div>` : ''}
            <div class="result-text">${esc(r.text)}</div>
          </div>`;
      }

      resultsArea.innerHTML = html;
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
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode())

        elif path == "/api/handles":
            handles = get_all_handles()
            contacts = extract_contacts()

            # Build a lookup: handle_id -> display name
            contact_map = {}
            for h, count in handles:
                contact_map[h] = get_name(h, contacts)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "handles": handles,
                "contacts": contact_map
            }).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/search":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            handles = body.get("handles", [])
            keywords_str = body.get("keywords", "")
            keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

            if not keywords:
                keywords = []

            result = search_messages(handles, keywords)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            self.send_response(404)
            self.end_headers()


def main():
    check_access()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n🔭 TextScope running at {url}")
    print("   Press Ctrl+C to stop\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 TextScope stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
