import os
import re
import json
from pathlib import Path
from flask import Flask, request, abort, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import dateparser
import dateparser.search

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# Import document loading and QA helpers from rag.py
try:
    from google import genai
except ImportError:
    genai = None

from rag import (
    DEFAULT_SOURCES,
    load_combined_document_context,
    answer_question
)

app = Flask(__name__)

# ── Twilio credentials ──────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")   # whatsapp:+14155238886
TWILIO_CALL_FROM      = os.getenv("TWILIO_CALL_FROM")          # verified number e.g. +14155238886
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY")

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
    raise EnvironmentError(
        "Missing Twilio credentials in .env. "
        "Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_NUMBER."
    )

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Gemini Client & Document Context Setup ──────────────────────────────────
gemini_client = None
if GEMINI_API_KEY and genai:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

DOCUMENT_CONTEXT = None

def get_document_context():
    """Lazy load and cache document context from Google Docs and local files."""
    global DOCUMENT_CONTEXT
    if DOCUMENT_CONTEXT is None:
        sources = list(DEFAULT_SOURCES)
        local_docx = Path(__file__).parent / "3. Classification (Module 2).docx"
        if local_docx.exists():
            sources.append(str(local_docx))
        print("[DOC LOAD] Fetching and loading document context...")
        DOCUMENT_CONTEXT = load_combined_document_context(sources)
        print(f"[DOC LOAD] Loaded {len(DOCUMENT_CONTEXT)} characters of document text.")
    return DOCUMENT_CONTEXT


# ── Scheduler ────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.start()


# ── Helpers ──────────────────────────────────────────────────────────────────
def message_to_json(req_form):
    """Convert Twilio webhook form data → clean JSON dict (unchanged)."""
    payload = {
        "message_sid":   req_form.get("MessageSid"),
        "from":          req_form.get("From"),
        "to":            req_form.get("To"),
        "body":          req_form.get("Body"),
        "timestamp":     datetime.utcnow().isoformat() + "Z",
        "profile_name":  req_form.get("ProfileName"),
        "num_media":     int(req_form.get("NumMedia", "0")),
    }
    if payload["num_media"] > 0:
        media = []
        for i in range(payload["num_media"]):
            media.append({
                "url":          req_form.get(f"MediaUrl{i}"),
                "content_type": req_form.get(f"MediaContentType{i}"),
            })
        payload["media"] = media
    return payload


def parse_reminder(text):
    """
    Detect natural language reminder requests like:
      - "remind me to tell xyz at 5:00 PM"
      - "remind me to tell John to send report in 10 minutes"
      - "remind me at 6pm to tell mom happy birthday"
      - "remind me in 30 mins to take medicine"
    Returns (reminder_text, datetime) or (None, None).
    """
    if not text:
        return None, None

    if "remind" not in text.lower():
        return None, None

    date_settings = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}

    # Pattern 1: "remind me [at|in|on] <time> (to|that|about)? <reminder>"
    p1 = r"remind\s+(?:me\s+)?(?:at|in|on)\s+(.+?)\s+(?:to|that|about)\s+(.+)"
    m1 = re.search(p1, text, re.IGNORECASE)
    if m1:
        time_str = m1.group(1).strip()
        reminder_text = m1.group(2).strip()
        parsed_time = dateparser.parse(time_str, settings=date_settings)
        if parsed_time:
            return reminder_text, parsed_time

    # Pattern 2: "remind me (to|that|about)? <reminder> [at|in|on] <time>"
    p2 = r"remind\s+(?:me\s+)?(?:to|that|about)?\s*(.+?)\s+(?:at|in|on)\s+(.+)"
    m2 = re.search(p2, text, re.IGNORECASE)
    if m2:
        reminder_text = m2.group(1).strip()
        time_str = m2.group(2).strip()
        parsed_time = dateparser.parse(time_str, settings=date_settings)
        if parsed_time:
            return reminder_text, parsed_time

    # Pattern 3: Fallback using dateparser.search.search_dates for general phrasing
    try:
        clean_prompt = re.sub(r"^remind\s+(?:me\s+)?(?:to|that|about)?\s*", "", text, flags=re.IGNORECASE).strip()
        found_dates = dateparser.search.search_dates(clean_prompt, settings=date_settings)
        if found_dates:
            date_str, parsed_time = found_dates[-1]
            reminder_text = clean_prompt.replace(date_str, "").strip()
            reminder_text = re.sub(r"\s+(?:at|in|on)$", "", reminder_text, flags=re.IGNORECASE).strip()
            if reminder_text and parsed_time:
                return reminder_text, parsed_time
    except Exception:
        pass

    return None, None



def make_reminder_call(to_number, reminder_text):
    """Place a Twilio voice call that speaks the reminder."""
    plain_number = to_number.replace("whatsapp:", "")
    call_from = TWILIO_CALL_FROM or TWILIO_WHATSAPP_NUMBER.replace("whatsapp:", "")

    twiml = VoiceResponse()
    twiml.say(
        f"Hello! This is your reminder. {reminder_text}. Have a great day!",
        voice="alice"
    )

    try:
        call = client.calls.create(
            twiml=str(twiml),
            to=plain_number,
            from_=call_from
        )
        print(f"[REMINDER CALL] SID={call.sid} → {plain_number}: '{reminder_text}'")
    except Exception as e:
        print(f"[REMINDER CALL ERROR] {e}")


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    if not request.form:
        abort(400)

    # 1. Log the full incoming message JSON
    json_payload = message_to_json(request.form)
    print(json.dumps(json_payload, indent=2))

    body      = request.form.get("Body", "").strip()
    from_num  = request.form.get("From", "")
    reply_text = ""

    # 2. Check for reminder command
    reminder_text, remind_at = parse_reminder(body)

    if reminder_text and remind_at:
        now = datetime.now()
        if remind_at > now:
            scheduler.add_job(
                make_reminder_call,
                "date",
                run_date=remind_at,
                args=[from_num, reminder_text],
                id=f"reminder_{from_num}_{remind_at.isoformat()}"
            )
            reply_text = (
                f"✅ Reminder set! I'll call you at "
                f"{remind_at.strftime('%I:%M %p')} to remind you: \"{reminder_text}\""
            )
            print(f"[REMINDER SET] '{reminder_text}' at {remind_at} for {from_num}")
        else:
            reply_text = "⚠️ That time is in the past! Please give a future time."
    else:
        # 3. Handle general document question using Gemini API with loaded document context
        if gemini_client and body:
            try:
                context = get_document_context()
                reply_text = answer_question(gemini_client, body, context)
            except Exception as e:
                print(f"[GEMINI QA ERROR] {e}")
                reply_text = "Sorry, an error occurred while searching the document."
        else:
            reply_text = "Received your message!"

    # 4. Send WhatsApp reply
    try:
        client.messages.create(
            body=reply_text,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=from_num
        )
    except Exception as e:
        print(f"[TWILIO REPLY ERROR] {e}")

    return "OK", 200


if __name__ == "__main__":
    # Pre-fetch document context on startup
    try:
        get_document_context()
    except Exception as e:
        print(f"[STARTUP WARN] Failed to pre-load documents: {e}")

    port = int(os.getenv("PORT", 5000))
    print(f"🚀 WhatsApp JSON + Reminder + Gemini Doc QA server running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
