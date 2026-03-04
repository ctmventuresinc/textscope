#!/usr/bin/env python3
"""
Cancel Link Finder

Scans your iMessage chat.db for messages with Mom containing links that have the word "cancel".
Searches across multiple phone numbers.
"""

import glob
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

IMESSAGE_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")

# Mom's phone numbers
MOM_PHONES = [
    "(718) 644-7589",
    "(212) 306-5272",
    "(212) 409-1840",
]

# Mom's email handles
TARGET_EMAILS = [
    "mayers1206@aol.com",
]

# Regex to find any URL
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
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
        sys.exit(1)


def q_params(sql, params):
    conn = sqlite3.connect(IMESSAGE_DB)
    r = conn.execute(sql, params).fetchall()
    conn.close()
    return r


def extract_contacts():
    contacts = {}
    db_paths = glob.glob(os.path.join(ADDRESSBOOK_DIR, "Sources", "*", "AddressBook-v22.abcddb"))
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


def normalize_phone(phone):
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else (digits if digits else None)


def html_escape(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


KEYWORDS = ["cancel", "unsubscribe", "instagram"]


def linkify_cancel_links(text):
    if not text:
        return ""
    text = html_escape(text)
    url_re = re.compile(r'(https?://[^\s<>&"\')\]]*)')

    def replacer(m):
        url = m.group(1)
        if any(kw in url.lower() for kw in KEYWORDS):
            return f'<a href="{url}" target="_blank" class="cancel-link">🔗 {url}</a>'
        return f'<a href="{url}" target="_blank" class="other-link">{url}</a>'

    return url_re.sub(replacer, text)


def main():
    check_access()
    contacts = extract_contacts()

    target_phones = set()
    for p in MOM_PHONES:
        norm = normalize_phone(p)
        if norm:
            target_phones.add(norm)

    print(f"\n🔍 Searching messages with Mom across {len(target_phones)} number(s)...")
    for p in target_phones:
        print(f"   • {p}")

    url_re = re.compile(URL_REGEX)

    # Query ALL messages so we get an accurate total count
    # Include attributedBody to catch URLs not in the text column
    sql = """
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
        WHERE m.text IS NOT NULL OR m.attributedBody IS NOT NULL
        ORDER BY (m.date/1000000000+978307200) ASC
    """
    rows = q_params(sql, ())

    results = []
    total_searched = 0
    for ts, handle, text, is_from_me, chat_name, attr_body in rows:
        handle_phone = normalize_phone(handle)
        # Also match by email handle
        handle_lower = (handle or "").lower()
        is_target = handle_phone in target_phones or any(
            e.lower() in handle_lower for e in TARGET_EMAILS
        )
        if not is_target:
            continue

        total_searched += 1

        # Extract text from attributedBody if text column is empty
        msg_text = text or ""
        if not msg_text and attr_body:
            try:
                part = attr_body.split(b"NSString")[1][5:]
                idx = part.find(b"NSDictionary")
                if idx > 0:
                    part = part[:idx]
                msg_text = part.decode("utf-8", errors="replace").strip()
            except Exception:
                pass

        # Also extract URLs from raw attributedBody bytes
        extra_urls = []
        if attr_body:
            extra_urls = [
                u.decode("utf-8", errors="replace")
                for u in re.findall(rb'https?://[^\x00-\x20\x7f-\x9f"<>]+', attr_body)
            ]

        # Find all URLs in the message text + attributedBody
        urls = url_re.findall(msg_text)
        all_urls = list(set(urls + extra_urls))
        # Keep only URLs containing any keyword
        matched_urls = [u for u in all_urls if any(kw in u.lower() for kw in KEYWORDS)]
        if not matched_urls:
            continue

        if not msg_text:
            msg_text = " ".join(matched_urls)

        sender_name = get_name(handle, contacts)
        dt_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")

        results.append({
            "dt": dt_str,
            "from_me": is_from_me,
            "sender": sender_name,
            "handle": handle,
            "links": matched_urls,
            "all_urls": all_urls,
            "text": msg_text,
            "chat": chat_name or "(1:1)",
        })

    contact_name = "Mom"
    output_file = os.path.join(os.path.dirname(__file__), "cancel_links.html")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"Link Finder - {contact_name}"

    rows_html = ""
    for i, r in enumerate(results):
        direction = "YOU → Mom" if r["from_me"] else f"Mom → YOU"
        direction_class = "from-me" if r["from_me"] else "from-them"

        rows_html += f"""
          <div class="row {direction_class}" data-row="{i}">
            <div class="meta">
              <div><b>{html_escape(r["dt"])}</b></div>
              <div class="direction {direction_class}">{html_escape(direction)}</div>
              <div class="muted">{html_escape(r["handle"])}</div>
              <div class="link-count">{len(r["links"])} cancel link(s)</div>
            </div>
            <div class="content">
              <div class="links">
        """

        for link in r["links"]:
            rows_html += f'<a href="{html_escape(link)}" target="_blank" class="cancel-link">🔗 {html_escape(link)}</a>\n'

        rows_html += f"""
              </div>
              <div class="text">{linkify_cancel_links(r["text"])}</div>
            </div>
          </div>
        """

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_escape(title)}</title>
  <style>
    body {{ margin:0; background:#0f0f10; }}
    .page {{
      max-width: 1100px;
      margin: 0 auto;
      background: #fbfbf7;
      min-height: 100vh;
      padding: 28px 18px 80px;
      font-family: "Times New Roman", Times, serif;
      color: #111;
    }}
    .mast {{
      border: 2px solid #111;
      padding: 14px 14px 10px;
      margin-bottom: 14px;
    }}
    .title {{
      font-size: 26px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .sub {{
      margin-top: 6px;
      font-family: Arial, sans-serif;
      font-size: 13px;
      color: #555;
      display:flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .row {{
      border: 1px solid #111;
      background: #fff;
      padding: 14px;
      margin-bottom: 12px;
      display: grid;
      grid-template-columns: 200px 1fr;
      gap: 16px;
      align-items: start;
    }}
    .row.from-me {{
      background: #e8f4f8;
    }}
    .row.from-them {{
      background: #f8f8f8;
    }}
    .meta {{
      font-family: Arial, sans-serif;
      font-size: 12px;
      color: #555;
      line-height: 1.5;
      word-break: break-word;
    }}
    .meta .direction {{
      font-weight: 700;
      padding: 4px 0;
      margin: 4px 0;
    }}
    .meta .direction.from-me {{
      color: #0066cc;
    }}
    .meta .direction.from-them {{
      color: #228B22;
    }}
    .meta .link-count {{
      margin-top: 8px;
      padding: 4px 8px;
      background: #fff;
      border: 1px solid #111;
      display: inline-block;
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
    }}
    .content {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .links {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .cancel-link {{
      display: inline-block;
      padding: 10px 12px;
      background: #d32f2f;
      color: #fff;
      text-decoration: none;
      border-radius: 4px;
      font-family: Arial, sans-serif;
      font-size: 13px;
      font-weight: 600;
      word-break: break-all;
      transition: background 0.2s;
    }}
    .cancel-link:hover {{
      background: #b71c1c;
    }}
    .other-link {{
      color: #0066cc;
      text-decoration: underline;
      word-break: break-all;
    }}
    .text {{
      font-size: 15px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
      padding: 12px;
      background: rgba(255,255,255,0.6);
      border-left: 3px solid #111;
    }}
    .text .cancel-link {{
      display: inline;
      padding: 2px 6px;
      font-size: 13px;
      border-radius: 2px;
    }}
    .muted {{ color: #555; }}
    .empty {{
      padding: 40px;
      text-align: center;
      font-family: Arial, sans-serif;
      color: #555;
      font-size: 16px;
    }}
    .phones {{
      font-family: Arial, sans-serif;
      font-size: 12px;
      color: #777;
      margin-top: 4px;
    }}
    @media (max-width: 720px) {{
      .row {{
        grid-template-columns: 1fr;
        gap: 12px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="mast">
      <div class="title">{html_escape(title)}</div>
      <div class="sub">
        <span>Messages searched: <b>{total_searched:,}</b></span>
        <span>Messages with matches: <b>{len(results):,}</b></span>
        <span>Total matching links: <b>{sum(len(r["links"]) for r in results):,}</b></span>
        <span>Generated: {generated_at}</span>
      </div>
      <div class="phones">Numbers searched: {", ".join(MOM_PHONES)}</div>
      <div class="phones">Keywords: {", ".join(KEYWORDS)}</div>
    </div>
    {rows_html if results else '<div class="empty">No links containing "cancel" found in messages with Mom.</div>'}
  </div>
</body>
</html>
"""

    with open(output_file, "w") as f:
        f.write(html)

    print(f"\n📊 Searched {total_searched:,} total messages with Mom")
    print(f"✅ Found {len(results)} message(s) with {sum(len(r['links']) for r in results)} matching link(s)")
    print(f"🔑 Keywords: {', '.join(KEYWORDS)}")
    print(f"📄 Saved to: {output_file}")
    subprocess.run(["open", output_file])
    print("🌐 Opening in browser...\n")


if __name__ == "__main__":
    main()
