import json
import os
import re
import sys
from typing import List, Dict, Any
import requests
from bse import BSE
from datetime import datetime

# Create downloads dir for BSE lib
os.makedirs('downloads', exist_ok=True)

# ----------------------------
# Config
# ----------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "REPLACE_ME"
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID") or "REPLACE_ME"

# Comma-separated BSE codes (e.g., "500325,532540") — leave empty for ALL
WATCHLIST_CODES = [s.strip() for s in os.environ.get("WATCHLIST_CODES", "").split(",") if s.strip()]  # Empty = all

# Max pages to scan each run (10 announcements/page typically)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))

# Cap how many messages to send per run (prevents 86 at once)
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))

# If state file is missing/corrupt (first run), just record latest ID and exit (no spam)
BOOTSTRAP_IF_EMPTY = os.environ.get("BOOTSTRAP_IF_EMPTY", "1") == "1"

STATE_FILE = "last_announcement.json"

# Stricter regex: Only actual results (avoid plain outcome/intimation noise)
RESULTS_RE = re.compile(
    r"(UNAUDITED\s*FINANCIAL\s*RESULTS|FINANCIAL\s*RESULTS|REG\s*\.?\s*33)",
    re.I,
)

# ----------------------------
# State
# ----------------------------
def load_state():
    """Return (last_id:int|None, is_bootstrap_needed:bool)."""
    if not os.path.exists(STATE_FILE):
        return (None, True)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        last_id = data.get("last_id", None)
        if last_id is None:
            return (None, True)
        return (int(last_id), False)
    except Exception as e:
        print(f"[warn] failed to read state: {e}", file=sys.stderr)
        return (None, True)

def save_state(last_id: int) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_id": int(last_id)}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[warn] failed to write state: {e}", file=sys.stderr)

# ----------------------------
# Fetch
# ----------------------------
def fetch_announcements(max_pages: int) -> List[Dict[str, Any]]:
    bse = BSE(download_folder='downloads')
    anns: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            # note: some versions use 'page' and others 'page_no'
            page_data = None
            try:
                page_data = bse.announcements(page_no=page)
            except TypeError:
                page_data = bse.announcements(page=page)
        except Exception as e:
            print(f"[warn] bse.announcements(page={page}) failed: {e}", file=sys.stderr)
            break
        if not page_data:
            break
        table = page_data.get('Table') if isinstance(page_data, dict) else page_data
        if not table:
            break
        anns.extend(table)
        if len(table) < 10:
            break
    print(f"[info] fetched {len(anns)} announcements")
    return anns

# ----------------------------
# Filters
# ----------------------------
def is_results_announcement(a: Dict[str, Any]) -> bool:
    newssub = (a.get("NEWSSUB", "") or "").upper()
    headline = (a.get("HEADLINE", "") or "").upper()
    text = newssub + " " + headline
    return bool(RESULTS_RE.search(text))

def parse_company_from_newssub(newssub: str) -> str:
    if not newssub:
        return "Unknown"
    parts = newssub.split('-', 1)
    return parts[0].strip() if parts else "Unknown"

def is_today_announcement(a: Dict[str, Any]) -> bool:
    news_dt = (a.get("NEWS_DT", "") or "").strip()
    if not news_dt:
        return False
    try:
        # NEWS_DT format like '2025-11-07T16:35:12.53'
        d = news_dt.split('T')[0]
        return d == datetime.now().strftime('%Y-%m-%d')
    except Exception:
        return False

def in_watchlist(a: Dict[str, Any]) -> bool:
    if not WATCHLIST_CODES:
        return True
    code = str(a.get("SCRIP_CD") or "").strip()
    return code in WATCHLIST_CODES

# ----------------------------
# Telegram
# ----------------------------
def tg_send(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID) or "REPLACE_ME" in (BOT_TOKEN + CHAT_ID):
        print("[warn] Telegram not configured; skipping send")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print(f"[warn] Telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Telegram send error: {e}", file=sys.stderr)

def build_message(a: Dict[str, Any]) -> str:
    company = parse_company_from_newssub(a.get("NEWSSUB", "")) or "Unknown Company"
    subject = (a.get("NEWSSUB", "") or a.get("HEADLINE", "") or "Corporate Announcement").strip()
    if len(subject) > 180:
        subject = subject[:177] + "..."
    date = a.get("NEWS_DT", "") or ""
    code = str(a.get("SCRIP_CD", "") or "")
    attachment = a.get("ATTACHMENTNAME", "")
    pdf_url = f"https://www.bseindia.com/xml-data/corpfiling_attachments/{attachment}" if attachment else ""
    url = a.get("URL", "") or pdf_url

    lines = [
        "🔔 New BSE Financial Result",
        f"🏢 Company: {company} ({code})" if code else f"🏢 Company: {company}",
        f"📝 Subject: {subject}",
    ]
    if date:
        lines.append(f"📅 Time: {date}")
    if url:
        lines.append(f"🔗 Link: {url}")
    return "\n".join(lines)

# ----------------------------
# Main
# ----------------------------
def main():
    last_id, need_bootstrap = load_state()

    anns = fetch_announcements(MAX_PAGES)
    if not anns:
        print("[info] no announcements fetched")
        return

    # Normalize IDs (prefer S_NO → SEQ_NO → id). Fallback to index order.
    norm = []
    fallback_counter = 1
    for a in anns:
        aid = None
        for key in ("S_NO", "SEQ_NO", "id"):
            v = a.get(key, None)
            if v is not None:
                try:
                    aid = int(str(v).strip())
                    break
                except Exception:
                    pass
        if aid is None:
            aid = fallback_counter
            fallback_counter += 1
        a["_id"] = aid
        norm.append(a)

    if not norm:
        print("[info] no normalized announcements with id")
        return

    # Sort ascending so we send oldest first
    norm.sort(key=lambda x: x["_id"])
    newest_id_this_run = max(x["_id"] for x in norm)

    # First-run bootstrap to avoid spamming 80+ old items
    if need_bootstrap and BOOTSTRAP_IF_EMPTY:
        save_state(newest_id_this_run)
        print(f"[info] bootstrap: recorded last_id={newest_id_this_run}, sent 0 alerts")
        return

    baseline = -1 if last_id is None else int(last_id)
    unseen = [a for a in norm if a["_id"] > baseline]

    # Filter to watchlist + results + today
    filtered = [a for a in unseen if in_watchlist(a) and is_results_announcement(a) and is_today_announcement(a)]

    # Cap per run to avoid flooding
    to_alert = filtered[:MAX_ALERTS_PER_RUN]

    # Optional: if we truncated, send a summary notice at the end
    truncated_count = max(0, len(filtered) - len(to_alert))

    sent = 0
    for a in to_alert:
        tg_send(build_message(a))
        sent += 1

    if truncated_count > 0:
        tg_send(f"⏳ More results detected: {truncated_count} additional announcement(s) held to avoid spam. They will be sent in the next cycles.")

    print(f"[info] sent {sent} alert(s); truncated={truncated_count}; unseen={len(unseen)}")

    # Advance state to newest seen id so we don't resend
    save_state(newest_id_this_run)

if __name__ == "__main__":
    main()
