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
# Config (hardcoded for direct run)
# ----------------------------
BOT_TOKEN = "8250933662:AAFp5oIujh2GxlNZLq2yXFyL4gKOsZm1mXI"
CHAT_ID = "7526048845"
# Comma-separated BSE codes (e.g., "500325,532540") — leave empty for ALL
WATCHLIST_CODES = [s.strip() for s in "".split(",") if s.strip()]  # Empty for all
# Max pages to scan each run (10 announcements per page typically)
MAX_PAGES = int("5")  # For recent coverage

STATE_FILE = "last_announcement.json"

# Stricter regex: Only actual "Financial Results" (skips intimation/outcome)
RESULTS_RE = re.compile(
    r"FINANCIAL\s*RESULT|UNAUDITED\s*FINANCIAL\s*RESULTS|REG\s*\.?\s*33",
    re.I,
)

def load_state() -> int:
    last_id = -1
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "last_id" in data:
                try:
                    last_id = int(data["last_id"])
                except (ValueError, TypeError):
                    last_id = -1
    except Exception as e:
        print(f"[warn] failed to read state: {e}", file=sys.stderr)
    return last_id

def save_state(last_id: int) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_id": int(last_id)}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[warn] failed to write state: {e}", file=sys.stderr)

def fetch_announcements(max_pages: int) -> List[Dict[str, Any]]:
    bse = BSE(download_folder='downloads')
    anns: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            page_data = bse.announcements(page_no=page)
        except Exception as e:
            print(f"[warn] bse.announcements(page_no={page}) failed: {e}", file=sys.stderr)
            break
        if not page_data or not page_data.get('Table', []):
            break
        page_anns = page_data.get('Table', [])
        anns.extend(page_anns)
        if len(page_anns) < 10:
            break
    print(f"[info] Fetched {len(anns)} announcements")
    return anns

def is_results_announcement(a: Dict[str, Any]) -> bool:
    # Targeted search in NEWSSUB and HEADLINE only (BSE keys)
    newssub = (a.get("NEWSSUB", "") or "").upper()
    headline = (a.get("HEADLINE", "") or "").upper()
    text = newssub + " " + headline
    matched = bool(RESULTS_RE.search(text))
    if matched:
        company = a.get("SCRIP_NAME", "Unknown") or parse_company_from_newssub(a.get("NEWSSUB", ""))
        print(f"[debug] MATCHED result announcement for {company}: {newssub[:100]}")  # Remove for production
    return matched

def parse_company_from_newssub(newssub: str) -> str:
    if not newssub:
        return "Unknown"
    # Parse first part before '-' (e.g., "WPIL Ltd" from "WPIL Ltd - 505872 - ...")
    parts = newssub.split('-', 1)
    return parts[0].strip() if parts else "Unknown"

def is_today_announcement(a: Dict[str, Any]) -> bool:
    # Check if announcement date is today (NEWS_DT format 'YYYY-MM-DDTHH:MM:SS.53')
    news_dt = a.get("NEWS_DT", "").strip()
    if not news_dt:
        return False
    today_str = datetime.now().strftime('%Y-%m-%d')
    # Trim to date part
    news_date_part = news_dt.split('T')[0]
    return news_date_part == today_str

def in_watchlist(a: Dict[str, Any]) -> bool:
    if not WATCHLIST_CODES:
        return True
    # BSE lib supplies 'SCRIP_CD'
    code = str(a.get("SCRIP_CD") or "").strip()
    return code in WATCHLIST_CODES

def tg_send(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID):
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
        print(f"[info] Sent to Telegram: {r.status_code}")  # Temp for confirmation
        if r.status_code != 200:
            print(f"[warn] Telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Telegram send error: {e}", file=sys.stderr)

def build_message(a: Dict[str, Any]) -> str:
    company = parse_company_from_newssub(a.get("NEWSSUB", "")) or "Unknown Company"
    subject = (a.get("NEWSSUB", "") or a.get("HEADLINE", "") or "Corporate Announcement").strip()
    if len(subject) > 100:
        subject = subject[:100] + "..."
    date = a.get("NEWS_DT", "") or ""
    code = str(a.get("SCRIP_CD", ""))
    # PDF URL: BSE standard for attachment
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
    elif pdf_url:
        lines.append(f"📄 PDF: {pdf_url}")
    return "\n".join(lines)

def main():
    last_id = load_state()
    # TEMP TEST: Force all as new (remove after test)
    last_id = -1
    print(f"[info] Temp last_id reset to {last_id} for test"
    
    anns = fetch_announcements(MAX_PAGES)
    if not anns:
        print("[info] no announcements fetched")
        return

    # Normalize IDs: Robust parsing with fallback sequential
    norm = []
    fallback_counter = 1
    for i, a in enumerate(anns):
        try:
            # Try BSE standard 'S_NO' first (string/float -> int)
            sno = a.get("S_NO")
            if sno is not None:
                aid = int(str(sno).strip())
            else:
                # Fallback to other fields
                aid = int(str(a.get("SEQ_NO") or a.get("id") or 0).strip())
            if aid > 0:
                a["_id"] = aid
                norm.append(a)
                continue
        except (ValueError, TypeError):
            pass  # Fall through to fallback
        # Fallback: Assign sequential ID based on index (assumes ordered by recency)
        a["_id"] = fallback_counter
        norm.append(a)
        fallback_counter += 1

    if not norm:
        print("[info] no normalized announcements with id")
        # Debug: Print sample to diagnose
        if anns:
            sample = anns[0]
            print(f"[debug] Sample announcement keys: {list(sample.keys())}")
            for k, v in sample.items():
                print(f"[debug]   {k}: {str(v)[:50]}...")
        return

    print(f"[info] Normalized {len(norm)} announcements")

    # Sort by ID ascending so we send oldest first (nice sequencing)
    norm.sort(key=lambda x: x["_id"])

    # Determine the newest id we saw this run (for state)
    newest_id_this_run = max(x["_id"] for x in norm)

    # Scan only the unseen ones
    unseen = [a for a in norm if a["_id"] > (last_id if last_id is not None else -1)]
    print(f"[debug] Len unseen: {len(unseen)}")  # TEMP DEBUG

    # Filter to watchlist + result-like announcements + today only
    to_alert = [a for a in unseen if in_watchlist(a) and is_results_announcement(a) and is_today_announcement(a)]

    # TEMP DEBUG: Print matched subjects (remove after test)
    if to_alert:
        subjects = [a.get("NEWSSUB", a.get("HEADLINE", "No subject")) for a in to_alert]
        print(f"[debug] Matched subjects: {subjects}")
    else:
        print("[debug] No matches: Check regex on subjects")
        # Print first unseen subject for check
        if unseen:
            print(f"[debug] Sample unseen subject: {unseen[0].get('NEWSSUB', unseen[0].get('HEADLINE', 'No subject'))}")

    if to_alert:
        for a in to_alert:
            tg_send(build_message(a))
        print(f"[info] sent {len(to_alert)} alert(s)")
    else:
        print("[info] no new results announcements to send")

    # Always advance state to newest seen, even if none matched filter,
    # so we don't reprocess the same IDs forever.
    save_state(newest_id_this_run)

if __name__ == "__main__":
    main()
