import os
import re
import json
from flask import Flask, request, abort, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import dateparser

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

app = Flask(__name__)

# ── Twilio credentials ──────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")   # whatsapp:+14155238886
TWILIO_CALL_FROM      = os.getenv("TWILIO_CALL_FROM")          # verified number e.g. +14155238886

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
    raise EnvironmentError(
        "Missing Twilio credentials in .env. "
        "Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_NUMBER."
    )

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

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
    Detect patterns like:
      "remind me to call mom at 3pm"
      "remind me take medicine at 14:30"
      "remind me meeting at 6 PM"
    Returns (reminder_text, datetime) or (None, None).
    """
    pattern = r"remind\s+me\s+(?:to\s+)?(.+?)\s+at\s+(.+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None, None

    reminder_text = match.group(1).strip()
    time_str      = match.group(2).strip()

    parsed_time = dateparser.parse(
        time_str,
        settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}
    )
    return reminder_text, parsed_time


def make_reminder_call(to_number, reminder_text):
    """Place a Twilio voice call that speaks the reminder."""
    # Strip 'whatsapp:' prefix to get a plain phone number
    plain_number = to_number.replace("whatsapp:", "")

    # Use TWILIO_CALL_FROM if set, otherwise strip whatsapp: from the sandbox number
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

    # 1. Always log the full JSON (original behaviour — untouched)
    json_payload = message_to_json(request.form)
    print(json.dumps(json_payload, indent=2))

    body      = request.form.get("Body", "")
    from_num  = request.form.get("From", "")
    reply_text = "Received your message!"

    # 2. Check for reminder command
    reminder_text, remind_at = parse_reminder(body)

    if reminder_text and remind_at:
        now = datetime.now()
        if remind_at > now:
            # Schedule the voice call
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

    # 3. Send WhatsApp reply
    client.messages.create(
        body=reply_text,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=from_num
    )

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 WhatsApp JSON + Reminder server running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
