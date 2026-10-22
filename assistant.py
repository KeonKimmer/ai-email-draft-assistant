"""
AI email draft assistant for inbound sales inquiries.

- Detects new website-form inquiry emails and generates AI draft replies (Claude)
- Captures the replies the team actually sent (not just drafts) into a Google Sheet "memory"
- Detects customer follow-up replies and drafts responses in the same Gmail thread
- Keyword-matches past Q&A pairs so the most relevant examples are used as context
- Drafts timed reminder emails when a customer goes quiet
- Runs once (for a scheduler such as GitHub Actions) or on a polling loop

All business-specific settings come from environment variables — see .env.example.
"""

import argparse
import base64
import random
import re
import time
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ── Google API call wrapper (rate-limit retry) ────────────────────────────────
def gexec(request, retries=6):
    """Execute a Google API request, backing off on 403/429/5xx rate-limit errors."""
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (403, 429, 500, 502, 503) and attempt < retries - 1:
                wait = (2 ** attempt) + random.random()
                print(f"  Google API {status} — retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            raise


# ── Configuration ──────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.drafts.create",
    "https://www.googleapis.com/auth/spreadsheets",
]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# ── Business settings (environment variables; see .env.example) ──────────────
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Acme Golf Simulators")
BUSINESS_DOMAIN = os.environ.get("BUSINESS_DOMAIN", "example.com")
SIGNER_NAME = os.environ.get("SIGNER_NAME", "Alex")
# Subject keyword shared by the form notification emails and their reply threads
SUBJECT_KEYWORD = os.environ.get("SUBJECT_KEYWORD", BUSINESS_NAME)
# Ignore mail older than this (Gmail search date, YYYY/MM/DD)
START_DATE = os.environ.get("START_DATE", "2026/01/01")

SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_RANGE = "Sheet1!A:C"  # columns: Customer Message | Our Reply | Date

# The contact form sends FROM one of these addresses; customer's real email is inside the HTML
FORM_SENDER = os.environ.get("FORM_SENDER", f"forms@{BUSINESS_DOMAIN}")
FORM_SENDER_NEW = os.environ.get("FORM_SENDER_ALT", f"inquiry@{BUSINESS_DOMAIN}")
FORM_SENDERS = {FORM_SENDER, FORM_SENDER_NEW}  # all valid form senders

# Team reply addresses — used to detect actual sent replies vs customer messages
TEAM_SENDERS = {
    s.strip() for s in os.environ.get("TEAM_SENDERS", f"inquiry@{BUSINESS_DOMAIN}").split(",") if s.strip()
}

# Gmail label applied to processed form emails to avoid re-processing
PROCESSED_LABEL = "Phase2-Processed"
# Gmail label applied to threads where we've already saved the team's sent reply to Sheet
REPLY_SAVED_LABEL = "Phase2-ReplySaved"
REMINDER_LABEL = "Phase2-Reminder1Sent"
# Gmail label applied to customer follow-up replies that have been processed
FOLLOWUP_LABEL = "Phase2-FollowupProcessed"
FOLLOWUP_REPLY_SAVED_LABEL = "Phase2-FollowupReplySaved"

FROM_EMAIL = os.environ.get("FROM_EMAIL", f"inquiry@{BUSINESS_DOMAIN}")

# Gmail search scope for incoming mail: read or unread, inbox or archived.
# Processing is tracked by our own labels, never by read/unread status.
RECEIVED_MAIL = "-in:sent -in:drafts -in:spam -in:trash"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# How many minutes between each poll when running in loop mode
POLL_INTERVAL_MINUTES = 5

# Small pause between Gmail calls inside loops (keeps us well under per-minute quota)
CALL_PAUSE = 0.3


# ── Auth ───────────────────────────────────────────────────────────────────────
def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return gmail, sheets


# ── Body helpers ───────────────────────────────────────────────────────────────
def get_html_body(payload):
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = get_html_body(part)
        if result:
            return result
    return ""


def get_plain_body(payload):
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = get_plain_body(part)
        if result:
            return result
    return ""


def clean_quoted_reply(text: str) -> str:
    """Strip quoted previous messages from a plain-text reply."""
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or line.strip().startswith("On ") and "wrote:" in line:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _headers_of(payload) -> dict:
    return {h["name"]: h["value"] for h in payload.get("headers", [])}


def _message_ids_of(payload) -> list[str]:
    return [h["value"] for h in payload.get("headers", []) if h["name"] in ("Message-ID", "Message-Id")]


# ── Form field parsing ─────────────────────────────────────────────────────────
def _extract_field(html: str, field_name: str) -> str:
    # Pattern 1: original format — <th>Field</th><td>Value</td>
    m = re.search(rf"{re.escape(field_name)}</th>\s*<td[^>]*>(.*?)</td>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    # Pattern 2: Brevo/table format — <td>Field</td><td>Value</td>
    m = re.search(rf"<td[^>]*>\s*{re.escape(field_name)}\s*</td>\s*<td[^>]*>(.*?)</td>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    # Pattern 3: label/value in any tag — Field: Value
    m = re.search(rf"{re.escape(field_name)}\s*:?\s*</[^>]+>\s*<[^>]+>\s*([^<]+)", html, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Pattern 4: plain colon-separated — Field: Value (plain text fallback)
    m = re.search(rf"{re.escape(field_name)}\s*:\s*([^\n<]+)", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_customer_email_fallback(html: str, plain: str) -> str:
    """Last-resort email extraction — find any non-team email address in the content."""
    content = html + " " + plain
    for email in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", content):
        if not any(d in email.lower() for d in [BUSINESS_DOMAIN, "brevosend.com", "sender-sib.com"]):
            return email
    return ""


# Each field -> label variants the form has used (or might use). The website form
# has renamed labels before (e.g. "Product" -> "Product Interest"), so add new
# variants here rather than changing the keys the rest of the code reads.
FORM_FIELD_ALIASES = {
    "First Name":   ["First Name", "Given Name"],
    "Last Name":    ["Last Name", "Surname", "Family Name"],
    "Email":        ["Email", "Email Address", "E-mail"],
    "Phone":        ["Phone", "Phone Number", "Mobile", "Telephone"],
    "State":        ["State", "State / Province", "Province", "Region"],
    "City":         ["City", "Town"],
    "Product":      ["Product Interest", "Product", "Products", "Product of Interest",
                     "Interested Product", "Interested In"],
    "Intended Use": ["Intended Use", "Intended Purpose"],
    "Message":      ["Message or Questions", "Message", "Questions", "Comments",
                     "Message or Question", "Your Message"],
}

# Fields worth a log warning when a real form email comes through without them
EXPECTED_FORM_FIELDS = ("First Name", "Email", "Product", "Intended Use", "Message")


def parse_form_fields(html: str, plain: str = "") -> dict:
    data = {}
    for field, aliases in FORM_FIELD_ALIASES.items():
        value = ""
        for source in (html, plain):
            for alias in aliases:
                value = _extract_field(source, alias)
                if value:
                    break
            if value:
                break
        data[field] = value
    # If Email still empty, fall back to any non-team address in the content
    if not data["Email"]:
        data["Email"] = _extract_customer_email_fallback(html, plain)
    data["Message or Questions"] = data["Message"]  # legacy key
    return data


def warn_missing_form_fields(data: dict):
    missing = [f for f in EXPECTED_FORM_FIELDS if not data.get(f)]
    if missing:
        print(f"  WARNING: form field(s) not found: {', '.join(missing)} — "
              f"the website form may have changed; update FORM_FIELD_ALIASES.")


# ── Per-cycle caches (reset in run_cycle) ─────────────────────────────────────
_label_cache: dict[str, str] = {}
_draft_threads_cache: set | None = None      # thread IDs that currently have a draft
_inquiry_thread_cache: dict[str, tuple[str, list[str]]] = {}  # customer email -> (thread_id, msg_ids)
_thread_cache: dict[str, dict] = {}          # thread_id -> full thread (format=full)


def reset_cycle_caches():
    global _draft_threads_cache
    _draft_threads_cache = None
    _inquiry_thread_cache.clear()
    _thread_cache.clear()


def get_thread_full(gmail, thread_id: str) -> dict:
    """Fetch a full thread once per cycle and cache it."""
    if thread_id not in _thread_cache:
        _thread_cache[thread_id] = gexec(
            gmail.users().threads().get(userId="me", id=thread_id, format="full")
        )
        time.sleep(CALL_PAUSE)
    return _thread_cache[thread_id]


# ── Gmail label helpers ────────────────────────────────────────────────────────
def get_or_create_label(gmail, name: str) -> str:
    if name in _label_cache:
        return _label_cache[name]
    labels = gexec(gmail.users().labels().list(userId="me")).get("labels", [])
    # Cache every label we see so later lookups cost nothing
    for lbl in labels:
        _label_cache[lbl["name"]] = lbl["id"]
    if name in _label_cache:
        return _label_cache[name]
    created = gexec(gmail.users().labels().create(userId="me", body={
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }))
    _label_cache[name] = created["id"]
    return created["id"]


def add_label(gmail, msg_id: str, label_id: str, also_mark_read: bool = False):
    body = {"addLabelIds": [label_id]}
    if also_mark_read:
        body["removeLabelIds"] = ["UNREAD"]
    gexec(gmail.users().messages().modify(userId="me", id=msg_id, body=body))


def get_thread_label_ids(gmail, thread_id: str) -> set:
    """Return all label IDs present on any message in the thread."""
    thread = gexec(gmail.users().threads().get(userId="me", id=thread_id, format="minimal"))
    ids = set()
    for msg in thread.get("messages", []):
        ids.update(msg.get("labelIds", []))
    return ids


# ── Google Sheets memory ───────────────────────────────────────────────────────
def fetch_all_qa_pairs(sheets) -> list[dict]:
    """Read all Q&A pairs from the Sheet."""
    try:
        result = gexec(sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=SHEET_RANGE
        ))
        rows = result.get("values", [])
    except HttpError as e:
        print(f"  Sheets read error: {e}")
        return []
    return [
        {"question": row[0], "answer": row[1]}
        for row in rows
        if len(row) >= 2 and row[0] and row[1]
    ]


def find_relevant_pairs(all_pairs: list[dict], query: str, top_n: int = 6) -> list[dict]:
    """
    Score every Q&A pair by keyword overlap with the query and return the top N.
    Falls back to the most recent pairs if no keywords match.
    """
    if not all_pairs:
        return []
    stop_words = {
        "i", "a", "an", "the", "is", "it", "to", "in", "and", "or", "for",
        "of", "my", "we", "do", "be", "me", "at", "on", "with", "this",
        "that", "are", "have", "has", "was", "but", "not", "you", "your",
    }
    query_words = {
        w.lower() for w in re.findall(r"\w+", query) if w.lower() not in stop_words
    }
    scored = []
    for pair in all_pairs:
        pair_words = {
            w.lower()
            for w in re.findall(r"\w+", pair["question"] + " " + pair["answer"])
            if w.lower() not in stop_words
        }
        score = len(query_words & pair_words)
        scored.append((score, pair))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [p for _, p in scored[:top_n] if _ > 0]
    # If nothing matched, fall back to most recent pairs
    if not top:
        top = all_pairs[-top_n:]
    return top


def find_row_by_question(sheets, question: str) -> int | None:
    """Return the 1-based row index of the first row whose column A matches question, or None."""
    try:
        result = gexec(sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="Sheet1!A:A"
        ))
        rows = result.get("values", [])
        for i, row in enumerate(rows, start=1):
            if row and row[0].strip() == question.strip():
                return i
    except HttpError as e:
        print(f"  Sheets read error: {e}")
    return None


def update_sheet_row(sheets, row_index: int, question: str, answer: str):
    """Overwrite columns A-C on an existing row."""
    range_notation = f"Sheet1!A{row_index}:C{row_index}"
    values = [[question, answer, datetime.now().strftime("%Y-%m-%d %H:%M")]]
    try:
        gexec(sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=range_notation,
            valueInputOption="RAW",
            body={"values": values},
        ))
        print(f"  Updated existing Sheet row {row_index} with actual sent reply.")
    except HttpError as e:
        print(f"  Sheets update error: {e}")
    time.sleep(1)


def append_to_sheet(sheets, question: str, answer: str, dry_run: bool = False):
    if dry_run:
        return
    values = [[question, answer, datetime.now().strftime("%Y-%m-%d %H:%M")]]
    try:
        gexec(sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=SHEET_RANGE,
            valueInputOption="RAW",
            body={"values": values},
        ))
        print(f"  Saved to Sheet memory.")
    except HttpError as e:
        print(f"  Sheets write error: {e}")
    time.sleep(1)


def save_or_update_sheet(sheets, question: str, answer: str, dry_run: bool = False):
    """Update existing row if question already exists, otherwise append a new row."""
    if dry_run:
        return
    existing_row = find_row_by_question(sheets, question)
    if existing_row:
        update_sheet_row(sheets, existing_row, question, answer)
    else:
        append_to_sheet(sheets, question, answer)


# ── Claude draft generation ────────────────────────────────────────────────────
def generate_draft_reply(fields: dict, relevant_pairs: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    customer_name = (
        f"{fields.get('First Name', '')} {fields.get('Last Name', '')}".strip() or "there"
    )
    customer_message = fields.get("Message", "") or fields.get("Message or Questions", "")
    product = fields.get("Product", "")
    intended_use = fields.get("Intended Use", "")
    city = fields.get("City", "")
    state = fields.get("State", "")
    past_examples = ""
    if relevant_pairs:
        examples_text = "\n\n".join(
            f"Customer asked: {p['question']}\nWe replied: {p['answer']}"
            for p in relevant_pairs
        )
        past_examples = (
            f"\n\nHere are the most relevant past team replies for style and content reference:\n\n"
            f"{examples_text}"
        )
    system_prompt = (
        f"You are {SIGNER_NAME} at {BUSINESS_NAME}, replying to a golf simulator sales inquiry. "
        "Write a professional, friendly, and detailed reply that:\n"
        "- Addresses the customer by their first name\n"
        "- Acknowledges their specific product interest and intended use\n"
        f"- Provides thorough information about {BUSINESS_NAME}'s golf simulators relevant to their needs\n"
        "- Mentions pricing, features, or specs if the past examples do so\n"
        "- Ends with a clear next step (schedule a call, request more info, etc.)\n"
        "- Matches the tone, style, spacing, and formatting of past team replies shown below exactly — "
        "same paragraph length, same line breaks, same sign-off format\n"
        "- Does NOT include a subject line — only the email body\n"
        f"- Signs off as '{SIGNER_NAME} | {BUSINESS_NAME}'\n\n"
        "HARD RULES — you must follow these without exception:\n"
        "- Do NOT promise or quote specific prices, delivery timelines, or installation dates — "
        "these must be confirmed by the team first. You may say pricing varies or to contact us for a quote.\n"
        "- Do NOT guarantee any revenue, ROI, or profit projections to the customer.\n"
        "- Keep the tone friendly, professional, and simple — avoid overpromising.\n"
        "- If critical information is missing (e.g. room dimensions for fit, intended use, "
        "or which product they want), politely ask for it rather than making assumptions."
        f"{past_examples}"
    )
    user_prompt = (
        f"Draft a reply to this new inquiry:\n\n"
        f"Customer Name: {customer_name}\n"
        f"Location: {city}, {state}\n"
        f"Product Interest: {product}\n"
        f"Intended Use: {intended_use}\n"
        f"Their Message: {customer_message}"
    )
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def generate_followup_reply(customer_message: str, relevant_pairs: list[dict]) -> str:
    """Generate a reply to a customer's follow-up message."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    past_examples = ""
    if relevant_pairs:
        examples_text = "\n\n".join(
            f"Customer asked: {p['question']}\nWe replied: {p['answer']}"
            for p in relevant_pairs
        )
        past_examples = (
            f"\n\nRelevant past team replies for reference:\n\n{examples_text}"
        )
    system_prompt = (
        f"You are {SIGNER_NAME} at {BUSINESS_NAME}, replying to a customer follow-up email. "
        "Write a professional, helpful reply that:\n"
        "- Directly addresses the customer's follow-up question or comment\n"
        f"- Provides thorough, useful information about {BUSINESS_NAME}'s golf simulators\n"
        "- Keeps the conversation moving toward a sale or next step\n"
        "- Matches the tone, style, spacing, and formatting of past team replies shown below exactly — "
        "same paragraph length, same line breaks, same sign-off format\n"
        "- Does NOT include a subject line — only the email body\n"
        f"- Signs off as '{SIGNER_NAME} | {BUSINESS_NAME}'"
        f"{past_examples}"
    )
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": f"Draft a reply to this customer follow-up:\n\n{customer_message}"}],
        system=system_prompt,
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


# ── Gmail draft helpers ───────────────────────────────────────────────────────
def thread_has_draft(gmail, thread_id: str) -> bool:
    """Return True if there is already a draft in this thread (drafts list fetched once per cycle)."""
    global _draft_threads_cache
    if _draft_threads_cache is None:
        # Page through every draft — list() returns only 100 per page
        _draft_threads_cache, page_token = set(), None
        while True:
            result = gexec(gmail.users().drafts().list(userId="me", maxResults=500, pageToken=page_token))
            _draft_threads_cache.update(d.get("message", {}).get("threadId") for d in result.get("drafts", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    return thread_id in _draft_threads_cache


# ── Gmail draft creation ───────────────────────────────────────────────────────
def create_gmail_draft(
    gmail,
    to_email: str,
    subject: str,
    body_text: str,
    thread_id: str,
    in_reply_to: str,
    references: str,
) -> str:
    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = references
    msg.attach(MIMEText(body_text, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    draft = gexec(gmail.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread_id}},
    ))
    # Keep the per-cycle draft cache accurate so we never create a duplicate this run
    if _draft_threads_cache is not None:
        _draft_threads_cache.add(thread_id)
    return draft.get("id", "")


# ── Thread lookup ─────────────────────────────────────────────────────────────
def find_original_inquiry_thread(gmail, customer_email: str) -> tuple[str, list[str]]:
    """
    Find the original form-email thread for this customer.
    Returns (thread_id, [all Message-IDs in thread]) so follow-up drafts can be
    anchored to the same thread, keeping the conversation in one place.

    Uses Gmail full-text search on the customer's email address so we only fetch
    a handful of candidate messages instead of scanning 50 full messages each call.
    Results are cached per cycle.
    """
    key = customer_email.lower()
    if key in _inquiry_thread_cache:
        return _inquiry_thread_cache[key]

    q = f"(from:{FORM_SENDER} OR from:{FORM_SENDER_NEW}) label:{PROCESSED_LABEL} \"{customer_email}\""
    result = gexec(gmail.users().messages().list(userId="me", q=q, maxResults=10))
    time.sleep(CALL_PAUSE)
    for mm in result.get("messages", []):
        try:
            m = gexec(gmail.users().messages().get(userId="me", id=mm["id"], format="full"))
            time.sleep(CALL_PAUSE)
            html = get_html_body(m.get("payload", {}))
            plain = get_plain_body(m.get("payload", {}))
            if not html and not plain:
                continue
            fields = parse_form_fields(html or "", plain or "")
            if fields.get("Email", "").lower() != key:
                continue
            tid = m.get("threadId", "")
            thread = gexec(gmail.users().threads().get(
                userId="me", id=tid, format="metadata", metadataHeaders=["Message-ID", "Message-Id"]
            ))
            time.sleep(CALL_PAUSE)
            msg_ids = []
            for tmsg in thread.get("messages", []):
                msg_ids.extend(_message_ids_of(tmsg.get("payload", {})))
            _inquiry_thread_cache[key] = (tid, msg_ids)
            return tid, msg_ids
        except Exception:
            continue
    _inquiry_thread_cache[key] = ("", [])
    return "", []


# ── Capture actual sent team replies into Sheet ─────────────────────────────────
def capture_sent_replies(gmail, sheets, dry_run: bool = False):
    """
    Find threads that have been processed (Phase2-Processed) but where the team's
    actual sent reply hasn't been saved to the Sheet yet (no Phase2-ReplySaved label).
    When found, save the real sent reply — not the draft — to the memory Sheet.
    """
    reply_saved_label_id = get_or_create_label(gmail, REPLY_SAVED_LABEL)
    query = f"label:{PROCESSED_LABEL} -label:{REPLY_SAVED_LABEL}"
    result = gexec(gmail.users().messages().list(userId="me", q=query, maxResults=20))
    messages = result.get("messages", [])
    if messages:
        print(f"Checking {len(messages)} thread(s) for actual sent team replies...")

    seen_threads = set()
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        thread_id = msg_meta.get("threadId", "")  # list() already returns threadId — no extra get()
        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        thread = get_thread_full(gmail, thread_id)
        thread_msgs = thread.get("messages", [])
        customer_msg_text = None
        team_sent_text = None
        for tmsg in thread_msgs:
            tpayload = tmsg.get("payload", {})
            theaders = _headers_of(tpayload)
            sender = theaders.get("From", "").lower()
            label_ids = tmsg.get("labelIds", [])
            # Customer's original inquiry (from form)
            if any(fs in sender for fs in FORM_SENDERS) and customer_msg_text is None:
                html = get_html_body(tpayload)
                plain = get_plain_body(tpayload)
                if html or plain:
                    fields = parse_form_fields(html or "", plain or "")
                    customer_msg_text = fields.get("Message", "")
            # the team's actual SENT reply (in Sent label, from a team address)
            if any(addr in sender for addr in TEAM_SENDERS) and "SENT" in label_ids:
                body = get_plain_body(tpayload) or re.sub(r"<[^>]+>", "", get_html_body(tpayload))
                cleaned = clean_quoted_reply(body)
                if cleaned and len(cleaned) > 20:
                    team_sent_text = cleaned
        if customer_msg_text and team_sent_text:
            print(f"  Found sent reply in thread {thread_id} — saving to Sheet.")
            save_or_update_sheet(sheets, customer_msg_text, team_sent_text, dry_run=dry_run)
            if not dry_run:
                add_label(gmail, msg_id, reply_saved_label_id)
        else:
            print(f"  No team sent reply found yet in thread {thread_id} — skipping.")
        time.sleep(CALL_PAUSE)

    # Also capture team sent replies after customer follow-ups
    followup_reply_saved_id = get_or_create_label(gmail, FOLLOWUP_REPLY_SAVED_LABEL)
    q2 = f"label:{FOLLOWUP_LABEL} -label:{FOLLOWUP_REPLY_SAVED_LABEL}"
    r2 = gexec(gmail.users().messages().list(userId="me", q=q2, maxResults=20))
    followup_msgs = r2.get("messages", [])
    if followup_msgs:
        print(f"Checking {len(followup_msgs)} follow-up message(s) for team sent replies...")

    seen_threads = set()
    for msg_meta in followup_msgs:
        msg_id = msg_meta["id"]
        thread_id = msg_meta.get("threadId", "")
        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        # One full-thread fetch gives us both the customer's follow-up and any team reply
        thread = get_thread_full(gmail, thread_id)
        msg = next((m for m in thread.get("messages", []) if m.get("id") == msg_id), None)
        if not msg:
            continue

        # Get customer's follow-up message text and timestamp
        payload = msg.get("payload", {})
        customer_follow_text = get_plain_body(payload) or re.sub(r"<[^>]+>", "", get_html_body(payload))
        customer_follow_text = clean_quoted_reply(customer_follow_text)
        if not customer_follow_text:
            customer_follow_text = (get_plain_body(payload) or re.sub(r"<[^>]+>", "", get_html_body(payload))).strip()[:500]
        customer_ts = int(msg.get("internalDate", 0)) / 1000

        # Find team sent reply that came AFTER the customer's follow-up
        team_reply_text = None
        for tmsg in thread.get("messages", []):
            tpayload = tmsg.get("payload", {})
            theaders = _headers_of(tpayload)
            sender = theaders.get("From", "").lower()
            label_ids = tmsg.get("labelIds", [])
            sent_ts = int(tmsg.get("internalDate", 0)) / 1000
            if (any(addr in sender for addr in TEAM_SENDERS)
                    and "SENT" in label_ids
                    and sent_ts > customer_ts):
                body = get_plain_body(tpayload) or re.sub(r"<[^>]+>", "", get_html_body(tpayload))
                cleaned = clean_quoted_reply(body)
                if cleaned and len(cleaned) > 20:
                    team_reply_text = cleaned

        # If not found in follow-up thread, also check the original inquiry thread
        # (drafts are anchored there, so the sent reply may live in that thread instead)
        if not team_reply_text:
            fu_headers = _headers_of(payload)
            cust_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", fu_headers.get("From", ""))
            if cust_match:
                orig_thread_id, _ = find_original_inquiry_thread(gmail, cust_match.group(0))
                if orig_thread_id and orig_thread_id != thread_id:
                    orig_thread = get_thread_full(gmail, orig_thread_id)
                    for tmsg in orig_thread.get("messages", []):
                        tpayload = tmsg.get("payload", {})
                        theaders = _headers_of(tpayload)
                        sender = theaders.get("From", "").lower()
                        label_ids = tmsg.get("labelIds", [])
                        sent_ts = int(tmsg.get("internalDate", 0)) / 1000
                        if (any(addr in sender for addr in TEAM_SENDERS)
                                and "SENT" in label_ids
                                and sent_ts > customer_ts):
                            body = get_plain_body(tpayload) or re.sub(r"<[^>]+>", "", get_html_body(tpayload))
                            cleaned = clean_quoted_reply(body)
                            if cleaned and len(cleaned) > 20:
                                team_reply_text = cleaned

        if customer_follow_text and team_reply_text:
            print(f"  Found follow-up sent reply in thread {thread_id} — saving to Sheet.")
            save_or_update_sheet(sheets, customer_follow_text, team_reply_text, dry_run=dry_run)
            if not dry_run:
                add_label(gmail, msg_id, followup_reply_saved_id)
        else:
            print(f"  No team follow-up sent reply yet in thread {thread_id} — skipping.")
        time.sleep(CALL_PAUSE)


# ── Process customer follow-up replies ─────────────────────────────────────────
def process_followup_replies(gmail, sheets, dry_run: bool = False):
    """
    Detect when a customer replies back and generate a new draft.
    We search inbox for customer messages with the inquiry subject line and check
    per-message (not per-thread) whether they've been processed already.
    Thread-level label filters would block all new messages in a thread once
    any one message in that thread is labeled — so we avoid them here.
    """
    followup_label_id = get_or_create_label(gmail, FOLLOWUP_LABEL)

    # Search all mail (read or unread, inbox or archived) for customer replies with the inquiry subject.
    # Exclude team/form senders in the query itself so we don't fetch them at all.
    q = (
        f"{RECEIVED_MAIL} after:{START_DATE} "
        f"subject:\"{SUBJECT_KEYWORD}\" "
        f"-from:{FORM_SENDER} -from:{FORM_SENDER_NEW} "
        + " ".join(f"-from:{s}" for s in TEAM_SENDERS)
    )
    result = gexec(gmail.users().messages().list(userId="me", q=q, maxResults=50))
    messages = result.get("messages", [])
    if messages:
        print(f"Checking {len(messages)} message(s) for customer follow-ups...")

    seen_msg_ids = set()
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if msg_id in seen_msg_ids:
            continue
        seen_msg_ids.add(msg_id)

        msg = gexec(gmail.users().messages().get(userId="me", id=msg_id, format="full"))
        time.sleep(CALL_PAUSE)

        # Per-message check: skip if this specific message was already processed
        msg_label_ids = set(msg.get("labelIds", []))
        if followup_label_id in msg_label_ids:
            continue

        payload = msg.get("payload", {})
        headers = _headers_of(payload)
        sender = headers.get("From", "").lower()
        thread_id = msg.get("threadId", "")

        # Belt-and-braces: skip if message is from the team or the automated form sender
        if any(addr in sender for addr in TEAM_SENDERS) or any(fs in sender for fs in FORM_SENDERS):
            continue

        # This is a customer message — treat it as a follow-up
        customer_reply_id = msg_id
        message_id_header = headers.get("Message-ID", "") or headers.get("Message-Id", "")
        subject = headers.get("Subject", f"Re: {BUSINESS_NAME}")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        customer_email = re.search(r"[\w.+-]+@[\w.-]+\.\w+", headers.get("From", ""))
        customer_email = customer_email.group(0) if customer_email else ""
        body = get_plain_body(payload) or re.sub(r"<[^>]+>", "", get_html_body(payload))
        customer_message = clean_quoted_reply(body)
        # Some clients put quoted history first — if stripping leaves nothing, use full body
        if not customer_message:
            customer_message = body.strip()[:2000]
        if not customer_message or not customer_email:
            continue

        # Try to anchor draft in the original inquiry thread so all replies
        # from this customer stay in one conversation in the business inbox.
        orig_thread_id, orig_msg_ids = find_original_inquiry_thread(gmail, customer_email)
        if orig_thread_id:
            draft_thread_id = orig_thread_id
            # References = full original chain + customer's current reply ID
            combined_ids = orig_msg_ids[:]
            if message_id_header and message_id_header not in combined_ids:
                combined_ids.append(message_id_header)
            references_chain = " ".join(combined_ids)
            if orig_thread_id != thread_id:
                print(f"  Anchoring draft to original thread {orig_thread_id}")
        else:
            draft_thread_id = thread_id
            full_thread = gexec(gmail.users().threads().get(
                userId="me", id=thread_id, format="metadata", metadataHeaders=["Message-ID", "Message-Id"]
            ))
            time.sleep(CALL_PAUSE)
            all_message_ids = []
            for tmsg in full_thread.get("messages", []):
                all_message_ids.extend(_message_ids_of(tmsg.get("payload", {})))
            references_chain = " ".join(all_message_ids)

        print(f"  Follow-up from {customer_email}: {customer_message[:80]}...".encode('ascii', errors='replace').decode())

        # Check for an existing draft BEFORE spending a Claude call
        if not dry_run and thread_has_draft(gmail, draft_thread_id):
            print(f"  Draft already exists in thread {draft_thread_id} — skipping.")
            add_label(gmail, customer_reply_id, followup_label_id, also_mark_read=True)
            continue

        all_pairs = fetch_all_qa_pairs(sheets)
        relevant = find_relevant_pairs(all_pairs, customer_message)
        draft_body = generate_followup_reply(customer_message, relevant)
        if not draft_body:
            print(f"  [ERROR] Claude returned empty follow-up reply")
            continue

        if dry_run:
            print("  ── FOLLOW-UP DRAFT PREVIEW ─────────────────────────────────────")
            print(f"  From:    {FROM_EMAIL}")
            print(f"  To:      {customer_email}")
            print(f"  Subject: {subject}")
            print()
            for line in draft_body.splitlines():
                print(f"  {line}")
            print("  ────────────────────────────────────────────────────────────────\n")
        else:
            draft_id = create_gmail_draft(
                gmail=gmail,
                to_email=customer_email,
                subject=subject,
                body_text=draft_body,
                thread_id=draft_thread_id,
                in_reply_to=message_id_header,
                references=references_chain,
            )
            print(f"  Follow-up draft created: {draft_id}")
            # Label before the Sheet write so a Sheet error can't cause a duplicate draft
            add_label(gmail, customer_reply_id, followup_label_id, also_mark_read=True)
            append_to_sheet(sheets, customer_message, draft_body)
        time.sleep(2)


# ── Spam detection ────────────────────────────────────────────────────────────
US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy",
}

SCAM_PHRASES = [
    "transfer of funds",
    "receive a funds transfer",
    "funds transfer",
    "wire transfer",
    "western union",
    "bitcoin",
    "cryptocurrency",
]


def is_spam(fields: dict) -> tuple[bool, str]:
    """Return (True, reason) if the inquiry looks like spam, (False, '') otherwise."""
    first = fields.get("First Name", "")
    last = fields.get("Last Name", "")
    email = fields.get("Email", "")
    state = fields.get("State", "").strip().lower()
    message = fields.get("Message", "")
    combined = f"{first} {last} {message}"
    # URL in name or message fields (never legitimate)
    if re.search(r"http[s]?://", combined, re.IGNORECASE):
        return True, "URL found in form fields"
    # Known scam phrases
    for phrase in SCAM_PHRASES:
        if phrase in combined.lower():
            return True, f"Scam phrase: '{phrase}'"
    # State is not a real US state (only flag if clearly wrong, not just abbreviated oddly)
    if state and len(state) > 2 and state not in US_STATES:
        return True, f"Invalid state: '{state}'"
    # Name contains digits (real names don't have numbers)
    if re.search(r"\d", first + last):
        return True, f"Name contains digits: '{first} {last}'"
    # Email domain is a known spam TLD
    if re.search(r"@[\w\-]+\.(xyz|top|loan|click|gq|tk|ml|cf|ga|web-library\.net)$", email, re.IGNORECASE):
        return True, f"Suspicious email domain: '{email}'"
    return False, ""


# ── New inquiry processing ─────────────────────────────────────────────────────
def process_new_inquiries(gmail, sheets, dry_run: bool = False):
    processed_label_id = None if dry_run else get_or_create_label(gmail, PROCESSED_LABEL)
    query = (
        f"{RECEIVED_MAIL} (from:{FORM_SENDER} OR from:{FORM_SENDER_NEW}) "
        f"subject:\"{SUBJECT_KEYWORD}\" -label:{PROCESSED_LABEL} after:{START_DATE}"
    )
    print(f"Searching for new inquiries: {query}")
    result = gexec(gmail.users().messages().list(userId="me", q=query, maxResults=25))
    messages = result.get("messages", [])
    print(f"Found {len(messages)} new inquiry email(s).\n")
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        try:
            _process_single_inquiry(gmail, sheets, msg_id, processed_label_id, dry_run)
        except Exception as e:
            print(f"  Error processing message {msg_id}: {e}")
        time.sleep(2)


def _process_single_inquiry(gmail, sheets, msg_id, processed_label_id, dry_run):
    msg = gexec(gmail.users().messages().get(userId="me", id=msg_id, format="full"))
    time.sleep(CALL_PAUSE)
    payload = msg.get("payload", {})
    headers = _headers_of(payload)
    thread_id = msg.get("threadId", "")
    message_id_header = headers.get("Message-ID", "") or headers.get("Message-Id", "")
    subject = headers.get("Subject", f"Re: {BUSINESS_NAME}")
    if not subject.startswith("Re:"):
        subject = f"Re: {subject}"

    html = get_html_body(payload)
    plain = get_plain_body(payload)
    if not html and not plain:
        print(f"  [SKIP] No body in {msg_id}")
        if not dry_run:
            add_label(gmail, msg_id, processed_label_id, also_mark_read=True)
        return

    fields = parse_form_fields(html or "", plain or "")
    customer_email = fields.get("Email", "").strip()
    if not customer_email:
        print(f"  [SKIP] No customer email found in {msg_id}")
        if not dry_run:
            add_label(gmail, msg_id, processed_label_id, also_mark_read=True)
        return

    # Spam check before doing anything else
    spam, reason = is_spam(fields)
    if spam:
        print(f"  [SPAM] Skipping {msg_id} — {reason}")
        return

    customer_message = fields.get("Message", "")
    customer_name = f"{fields.get('First Name', '')} {fields.get('Last Name', '')}".strip()
    print(f"  {'[DRY RUN] ' if dry_run else ''}Inquiry from: {customer_name} <{customer_email}>")
    print(f"  Product:      {fields.get('Product', 'N/A')}")
    warn_missing_form_fields(fields)
    print(f"  Location:     {fields.get('City', '')}, {fields.get('State', '')}")
    print(f"  Message:      {customer_message[:120]}...")
    print()

    # Skip if a draft already exists in this thread — check BEFORE spending a Claude call
    if not dry_run and thread_has_draft(gmail, thread_id):
        print(f"  Draft already exists in thread {thread_id} — skipping.")
        add_label(gmail, msg_id, processed_label_id, also_mark_read=True)
        return

    # Find the most relevant past Q&A pairs by keyword match
    all_pairs = fetch_all_qa_pairs(sheets)
    relevant = find_relevant_pairs(all_pairs, customer_message + " " + fields.get("Product", ""))
    print(f"  Using {len(relevant)} relevant past Q&A pair(s) as context.")

    draft_body = generate_draft_reply(fields, relevant)
    if not draft_body:
        print(f"  [ERROR] Claude returned empty reply for {msg_id}")
        return

    if dry_run:
        print("  ── DRAFT REPLY PREVIEW ─────────────────────────────────────────")
        print(f"  From:    {FROM_EMAIL}")
        print(f"  To:      {customer_email}")
        print(f"  Subject: {subject}")
        print()
        for line in draft_body.splitlines():
            print(f"  {line}")
        print("  ────────────────────────────────────────────────────────────────")
        print("  [DRY RUN] Nothing saved.\n")
        return

    # Build full references chain so Gmail threads the reply correctly
    full_thread = gexec(gmail.users().threads().get(
        userId="me", id=thread_id, format="metadata", metadataHeaders=["Message-ID", "Message-Id"]
    ))
    time.sleep(CALL_PAUSE)
    all_message_ids = []
    for tmsg in full_thread.get("messages", []):
        all_message_ids.extend(_message_ids_of(tmsg.get("payload", {})))
    references_chain = " ".join(all_message_ids)

    draft_id = create_gmail_draft(
        gmail=gmail,
        to_email=customer_email,
        subject=subject,
        body_text=draft_body,
        thread_id=thread_id,
        in_reply_to=message_id_header,
        references=references_chain,
    )
    print(f"  Draft created: {draft_id}")
    # Label immediately so a Sheet error below can never cause a second draft next run
    add_label(gmail, msg_id, processed_label_id, also_mark_read=True)
    print(f"  Marked as processed.")

    # Save the draft to Sheet as a placeholder — will be overwritten by actual sent reply later
    if customer_message:
        append_to_sheet(sheets, customer_message, draft_body)


# ── Reminder follow-up ────────────────────────────────────────────────────────
def business_days_since(timestamp_seconds: float) -> int:
    """Count business days (Mon–Fri) elapsed since a Unix timestamp."""
    sent = datetime.fromtimestamp(timestamp_seconds)
    now = datetime.now()
    days = 0
    current = sent
    while current.date() < now.date():
        current += timedelta(days=1)
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            days += 1
    return days


def generate_reminder_draft(customer_first_name: str, product: str) -> tuple[str, str]:
    subject = f"Re: {BUSINESS_NAME} — Following Up"
    body = (
        f"Hi {customer_first_name},\n\n"
        f"This is just a friendly reminder — I wanted to check if you're still interested in {product}.\n\n"
        f"If you have any questions, please feel free to reach out and ask.\n\n"
        f"Thank you, and I look forward to hearing from you soon.\n\n"
        f"Best,\n"
        f"{SIGNER_NAME}\n\n"
        f"{BUSINESS_NAME}"
    )
    return subject, body


def process_reminders(gmail, sheets, dry_run: bool = False):
    """
    For threads where team sent a reply (Phase2-ReplySaved) but the customer
    hasn't replied back (no Phase2-FollowupProcessed) and 3+ business days
    have passed, create one reminder draft. Only one reminder per thread ever.
    """
    reminder_label_id = get_or_create_label(gmail, REMINDER_LABEL)
    # Threads where we saved a sent reply but no customer reply yet, and no reminder yet
    query = f"label:{REPLY_SAVED_LABEL} -label:{FOLLOWUP_LABEL} -label:{REMINDER_LABEL}"
    result = gexec(gmail.users().messages().list(userId="me", q=query, maxResults=20))
    messages = result.get("messages", [])
    if messages:
        print(f"Checking {len(messages)} thread(s) for reminder eligibility...")

    seen_threads = set()
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        thread_id = msg_meta.get("threadId", "")
        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        thread = get_thread_full(gmail, thread_id)
        thread_msgs = thread.get("messages", [])

        customer_first_name = ""
        product = ""
        customer_email = ""
        last_team_sent_time = None
        last_team_msg_id = ""
        all_thread_msg_ids = []
        for tmsg in thread_msgs:
            tpayload = tmsg.get("payload", {})
            theaders = _headers_of(tpayload)
            sender = theaders.get("From", "").lower()
            label_ids = tmsg.get("labelIds", [])
            all_thread_msg_ids.extend(_message_ids_of(tpayload))
            # Parse customer name, product, and email from the original form email
            if any(fs in sender for fs in FORM_SENDERS) and not customer_first_name:
                html = get_html_body(tpayload)
                plain = get_plain_body(tpayload)
                if html or plain:
                    fields = parse_form_fields(html or "", plain or "")
                    customer_first_name = fields.get("First Name", "").strip() or "there"
                    product = fields.get("Product", "").strip() or "our golf simulator"
                    customer_email = fields.get("Email", "").strip()
            # Track the most recent team sent message time
            if any(addr in sender for addr in TEAM_SENDERS) and "SENT" in label_ids:
                sent_time = int(tmsg.get("internalDate", 0)) / 1000
                if last_team_sent_time is None or sent_time > last_team_sent_time:
                    last_team_sent_time = sent_time
                    ids = _message_ids_of(tpayload)
                    if ids:
                        last_team_msg_id = ids[-1]

        if not last_team_sent_time:
            continue  # The team hasn't sent anything yet

        bdays = business_days_since(last_team_sent_time)
        if bdays < 3:
            print(f"  Thread {thread_id}: only {bdays} business day(s) since last reply — not yet.")
            continue

        if not customer_email:
            continue

        # Check if customer has replied in ANY thread before creating reminder
        customer_replied = gexec(gmail.users().messages().list(
            userId="me",
            q=f"from:{customer_email} after:{START_DATE} subject:\"{SUBJECT_KEYWORD}\"",
            maxResults=1
        ))
        time.sleep(CALL_PAUSE)
        if customer_replied.get("messages"):
            print(f"  {customer_email} already replied in another thread — skipping reminder.")
            continue

        print(f"  Reminder due for {customer_email} ({bdays} business days, no reply).")
        subject, body = generate_reminder_draft(customer_first_name, product)
        if not subject or not body:
            print(f"  [ERROR] Empty reminder for thread {thread_id}")
            continue

        references_chain = " ".join(all_thread_msg_ids)
        if not dry_run and thread_has_draft(gmail, thread_id):
            print(f"  Reminder draft already exists in thread {thread_id} — skipping.")
            continue

        if dry_run:
            print(f"  [DRY RUN] Reminder subject: {subject}")
            print(f"  [DRY RUN] Reminder body: {body[:120]}...")
        else:
            draft_id = create_gmail_draft(
                gmail=gmail,
                to_email=customer_email,
                subject=subject,
                body_text=body,
                thread_id=thread_id,
                in_reply_to=last_team_msg_id,
                references=references_chain,
            )
            print(f"  Reminder draft created: {draft_id}")
            add_label(gmail, msg_id, reminder_label_id)
        time.sleep(2)


# ── Full run cycle ─────────────────────────────────────────────────────────────
def run_cycle(gmail, sheets, dry_run: bool = False):
    reset_cycle_caches()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  Email Draft Assistant — {now}{' [DRY RUN]' if dry_run else ''}")
    print(f"{'='*60}\n")
    process_new_inquiries(gmail, sheets, dry_run=dry_run)
    print()
    process_followup_replies(gmail, sheets, dry_run=dry_run)
    print()
    capture_sent_replies(gmail, sheets, dry_run=dry_run)
    print()
    process_reminders(gmail, sheets, dry_run=dry_run)
    print(f"\nCycle complete.")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AI email draft assistant")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving anything")
    parser.add_argument("--loop", action="store_true", help=f"Poll every {POLL_INTERVAL_MINUTES} min continuously")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        raise EnvironmentError("Set the ANTHROPIC_API_KEY environment variable before running.")

    print("Authenticating...")
    gmail, sheets = authenticate()
    print("Authenticated.\n")

    if args.loop:
        print(f"Running in loop mode — polling every {POLL_INTERVAL_MINUTES} minutes. Ctrl+C to stop.\n")
        while True:
            try:
                run_cycle(gmail, sheets, dry_run=args.dry_run)
            except Exception as e:
                print(f"Cycle error: {e}")
            print(f"Sleeping {POLL_INTERVAL_MINUTES} min until next check...")
            time.sleep(POLL_INTERVAL_MINUTES * 60)
    else:
        run_cycle(gmail, sheets, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
