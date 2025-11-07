import json
import os
import re
import sys
from typing import List, Dict, Any
import requests
from bse import BSE
from datetime import datetime, timedelta

# Create downloads dir for BSE lib
os.makedirs('downloads', exist_ok=True)

# ----------------------------
# Config via environment
# ----------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Comma-separated BSE codes (e.g., "500325,532540") — leave empty for ALL
WATCHLIST_CODES = [s.strip() for s in os.environ.get("WATCHLIST_CODES", "").split(",") if s.strip()]
# Max pages to scan each run (10 announcements per page typically)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))

STATE_FILE = "last_announcement.json"

# Regex for detecting "results" / Reg-33 style filings (subject/title text)
RESULTS_RE = re.compile(
    r"(RESULT|FINANCIAL|REG[\s\.]*33|Q[1-4]\s*FY|QUARTER|UNAUDITED|AUDITED|YEAR\s*ENDED|STATEMENT\s+OF\s+STANDALONE|CONSOLIDATED)",
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
    # Fetch recent: last 7 days for coverage (adjust if needed)
    from_date = datetime.now() - timedelta(days=7)
    bse = BSE(download_folder='downloads')
    anns: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            page_data = bse.announcements(
                page_no=page,
                from_date=from_date,
                segment='equity'  # Focus on equity; change if needed
            )
        except Exception as e:
            print(f"[warn] bse.announcements(page_no={page}) failed: {e}", file=sys.stderr)
            break
        if not page_data or not page_data.get('Table'):
            break
        anns.extend(page_data['Table'])
        # Heuristic: if fewer than 10, probably last page
        if len(page_data['Table']) < 10:
            break
    return anns

def is_results_announcement(a: Dict[str, Any]) -> bool:
    # Check subject + any available text fields (BSE-specific keys)
    fields = []
    for key in ("ANN_TITLE", "ANN_TYPE", "description", "title"):
        v = (a.get(key) or "").strip()
        if v:
            fields.append(v)
    text = " | ".join(fields)
    return bool(RESULTS_RE.search(text))

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
        "text": text,  # keep simple; no parse_mode to avoid escaping issues
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print(f"[warn] Telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Telegram send error: {e}", file=sys.stderr)

def build_message(a: Dict[str, Any]) -> str:
    company = a.get("SCRIP_NAME") or a.get("company") or "Unknown Company"
    subject = (a.get("ANN_TITLE") or a.get("subject") or "Corporate Announcement").strip()
    if len(subject) > 100:
        subject = subject[:100] + "..."
    date = a.get("ANN_DT") or a.get("date") or ""
    code = a.get("SCRIP_CD") or ""
    # Links (BSE often has PDF_URL or attachment)
    pdf_url = a.get("PDF_URL") or a.get("attachment") or ""
    url = a.get("URL") or a.get("link") or pdf_url

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
    anns = fetch_announcements(MAX_PAGES)
    if not anns:
        print("[info] no announcements fetched")
        return

    # Normalize IDs: Try common BSE ID fields (S_NO is sequential)
    norm = []
    for a in anns:
        try:
            aid = int(
                a.get("S_NO") or
                a.get("SEQ_NO") or
                a.get("id") or
                0
            )
            if aid == 0:
                raise ValueError("No valid ID")
        except Exception:
            continue
        a["_id"] = aid
        norm.append(a)

    if not norm:
        print("[info] no normalized announcements with id")
        return

    # Sort by ID ascending so we send oldest first (nice sequencing)
    norm.sort(key=lambda x: x["_id"])

    # Determine the newest id we saw this run (for state)
    newest_id_this_run = max(x["_id"] for x in norm)

    # Scan only the unseen ones
    unseen = [a for a in norm if a["_id"] > (last_id if last_id is not None else -1)]

    # Filter to watchlist + result-like announcements
    to_alert = [a for a in unseen if in_watchlist(a) and is_results_announcement(a)]

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
    main()            print(f"[warn] Telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Telegram send error: {e}", file=sys.stderr)

def build_message(a: Dict[str, Any]) -> str:
    company = a.get("SCRIP_NAME") or a.get("company") or "Unknown Company"
    subject = (a.get("ANN_TITLE") or a.get("subject") or "Corporate Announcement").strip()
    if len(subject) > 100:
        subject = subject[:100] + "..."
    date = a.get("ANN_DT") or a.get("date") or ""
    code = a.get("SCRIP_CD") or ""
    # Links (BSE often has PDF_URL or attachment)
    pdf_url = a.get("PDF_URL") or a.get("attachment") or ""
    url = a.get("URL") or a.get("link") or pdf_url

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
    anns = fetch_announcements(MAX_PAGES)
    if not anns:
        print("[info] no announcements fetched")
        return

    # Normalize IDs: Try common BSE ID fields (S_NO is sequential)
    norm = []
    for a in anns:
        try:
            aid = int(
                a.get("S_NO") or
                a.get("SEQ_NO") or
                a.get("id") or
                0
            )
            if aid == 0:
                raise ValueError("No valid ID")
        except Exception:
            continue
        a["_id"] = aid
        norm.append(a)

    if not norm:
        print("[info] no normalized announcements with id")
        return

    # Sort by ID ascending so we send oldest first (nice sequencing)
    norm.sort(key=lambda x: x["_id"])

    # Determine the newest id we saw this run (for state)
    newest_id_this_run = max(x["_id"] for x in norm)

    # Scan only the unseen ones
    unseen = [a for a in norm if a["_id"] > (last_id if last_id is not None else -1)]

    # Filter to watchlist + result-like announcements
    to_alert = [a for a in unseen if in_watchlist(a) and is_results_announcement(a)]

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
    main()    if len(subject) > 100:
        subject = subject[:100] + "..."
    date = a.get("date") or a.get("datetime") or ""
    code = a.get("scrip_code") or a.get("security_code") or ""
    # Links (whatever is available)
    pdf_url = a.get("pdf_url") or a.get("attachment") or ""
    url = a.get("url") or a.get("link") or pdf_url

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
    anns = fetch_announcements(MAX_PAGES)
    if not anns:
        print("[info] no announcements fetched")
        return

    # Normalize IDs, keep only those that have numeric id
    norm = []
    for a in anns:
        try:
            aid = int(a.get("id"))
        except Exception:
            continue
        a["_id"] = aid
        norm.append(a)

    if not norm:
        print("[info] no normalized announcements with id")
        return

    # Sort by ID ascending so we send oldest first (nice sequencing)
    norm.sort(key=lambda x: x["_id"])

    # Determine the newest id we saw this run (for state)
    newest_id_this_run = max(x["_id"] for x in norm)

    # Scan only the unseen ones
    unseen = [a for a in norm if a["_id"] > (last_id if last_id is not None else -1)]

    # Filter to watchlist + result-like announcements
    to_alert = [a for a in unseen if in_watchlist(a) and is_results_announcement(a)]

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
