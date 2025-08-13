import os
import json
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional, List

import requests
from flask import Flask, request, jsonify, render_template
from twilio.twiml.messaging_response import MessagingResponse

from ciao_booking_client import CiaoBookingClient
from utils import normalize_sender, clamp_history, extract_reservation_id

# ---------------- Logging ----------------
_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
    _level = "INFO"
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------- Flask ----------------
app = Flask(__name__)

# ---------------- OpenAI via requests ----------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_CHAT_URL = f"{OPENAI_BASE}/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

def call_openai_chat(messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        logger.debug("OpenAI payload: %s", payload)
        r = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        return content
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return "Mi dispiace, ho un problema tecnico momentaneo. Riprova tra poco."

# ---------------- Knowledge Base ----------------
def load_kb() -> Dict[str, Any]:
    path = os.path.join(os.getcwd(), "knowledge_base.json")
    if not os.path.exists(path):
        logger.warning("knowledge_base.json non trovato, uso KB vuota.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

KB = load_kb()

def choose_video_link(property_name: str) -> Optional[str]:
    if not property_name:
        return None
    p = property_name.strip().lower()
    for k, v in (KB.get("videos") or {}).items():
        if k.lower() == p:
            return v
    return None

# ---------------- CiaoBooking ----------------
CB = CiaoBookingClient(
    base_url="https://api.ciaobooking.com",
    email=os.environ.get("CIAOBOOKING_EMAIL", ""),
    password=os.environ.get("CIAOBOOKING_PASSWORD", ""),
    locale=os.environ.get("CIAOBOOKING_LOCALE", "it"),
)

# ---------------- Memoria in RAM ----------------
# session_store[phone] = {"history":[...], "booking_ctx":{...}, "created_at": iso}
session_store: Dict[str, Dict[str, Any]] = {}

def get_session(phone: str) -> Dict[str, Any]:
    sess = session_store.get(phone)
    if not sess:
        sess = {"history": [], "created_at": datetime.utcnow().isoformat()}
        session_store[phone] = sess
    return sess

# ---------------- Booking context helpers ----------------
STATUS_MAP = {1: "CANCELED", 2: "CONFIRMED", 3: "PENDING"}
GUEST_STATUS_MAP = {0: "NOT_ARRIVED", 1: "ARRIVED", 2: "LEFT"}
CHECKIN_DONE_MAP = {0: "TO_DO", 1: "COMPLETED", 2: "VERIFIED"}

def _parse_ymd(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:
            return None

def should_offer_checkin_assets(booking_ctx: Dict[str, Any]) -> bool:
    res = (booking_ctx.get("reservation") or {})
    if not res:
        return False
    if res.get("status") != "CONFIRMED":
        return False
    if res.get("guest_status") not in ("NOT_ARRIVED",):
        return False
    if res.get("is_checkin_completed") not in ("TO_DO",):
        return False
    d_start = _parse_ymd(res.get("start_date"))
    d_end = _parse_ymd(res.get("end_date"))
    if not d_start:
        return False
    today = date.today()
    if today == d_start:
        return True
    if d_end and d_start <= today < d_end:
        return True
    return False

def build_booking_context(phone: str, user_text: str) -> Dict[str, Any]:
    sess = get_session(phone)
    if "booking_ctx" in sess:
        return sess["booking_ctx"]

    ctx: Dict[str, Any] = {}
    # 1) prova da reservation id nel testo
    res_id = extract_reservation_id(user_text)
    if res_id:
        res = CB.get_reservation_by_id(res_id)
        if res:
            status_i = res.get("status")
            guest_status_i = res.get("guest_status")
            checkin_i = res.get("is_checkin_completed")

            ctx["reservation"] = {
                "id": res.get("id"),
                "status_code": status_i,
                "status": STATUS_MAP.get(status_i, str(status_i)),
                "guest_status_code": guest_status_i,
                "guest_status": GUEST_STATUS_MAP.get(guest_status_i, str(guest_status_i)),
                "is_checkin_completed_code": checkin_i,
                "is_checkin_completed": CHECKIN_DONE_MAP.get(checkin_i, str(checkin_i)),
                "start_date": res.get("start_date"),
                "end_date": res.get("end_date"),
                "property_id": res.get("property_id"),
                "room_type_id": res.get("room_type_id"),
                "guests": res.get("guests"),
                "arrival_time": res.get("arrival_time"),
                "checkout_time": res.get("checkout_time"),
            }
            prop = None
            if res.get("property_id"):
                prop = CB.get_property(res["property_id"])
            if prop:
                ctx["property"] = {"id": prop.get("id"), "name": prop.get("name")}
            sess["booking_ctx"] = ctx
            return ctx

    # 2) client da telefono
    cli = CB.get_client_by_phone(phone)
    if cli:
        ctx["client"] = {
            "id": cli.get("id"),
            "name": cli.get("name"),
            "phone": cli.get("phone"),
        }
        sess["booking_ctx"] = ctx
        return ctx

    sess["booking_ctx"] = ctx
    return ctx

# ---------------- Prompt ----------------
BASE_SYSTEM_PROMPT = """
Sei un assistente per strutture ricettive a Pisa.
Stile: chiaro, cordiale, conciso. Fai al massimo UNA domanda mirata quando serve.
Usa SOLO la Knowledge Base (KB) e gli eventuali dati di prenotazione forniti in CONTEXT; non inventare.

Regole:
- Se CONTEXT.reservation.status = CONFIRMED e (guest_status = NOT_ARRIVED) e (is_checkin_completed = TO_DO)
  e oggi è il giorno di arrivo (o soggiorno in corso), fornisci PROATTIVAMENTE i link di self check‑in/accesso
  dalla KB relativi alla property, senza aspettare che l’utente li chieda.
- *Taxi/Transfer*: chiedi solo i dati mancanti tra persone, orario, partenza, destinazione.
  Tariffe: Aeroporto↔Città 50€, altrimenti 40€.
- *Parcheggio*: se manca la struttura, chiedila e poi rispondi in base alla KB.
- *Video/Accesso/Corrente*: se c’è la struttura nel CONTEXT o viene indicata dall’utente, restituisci il link corretto dalla KB.
- Se l’utente invia '/reset', conferma il reset e basta.

Knowledge Base (JSON):
""".strip()

def build_system_message(kb: Dict[str, Any], booking_ctx: Dict[str, Any]) -> Dict[str, str]:
    kb_json = json.dumps(kb, ensure_ascii=False, indent=2)
    ctx_json = json.dumps(booking_ctx, ensure_ascii=False)
    content = f"{BASE_SYSTEM_PROMPT}\n{kb_json}\n\nCONTEXT:\n{ctx_json}"
    return {"role": "system", "content": content}

# ---------------- Risposta assistente ----------------
def make_assistant_reply(phone: str, user_text: str) -> str:
    sess = get_session(phone)

    # reset
    if user_text.strip().lower() == "/reset":
        session_store.pop(phone, None)
        return "✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Video/Accesso)"

    # context
    booking_ctx = build_booking_context(phone, user_text)

    # history (max)
    hist = clamp_history(sess.get("history", []), max_pairs=6)

    messages = [build_system_message(KB, booking_ctx)] + hist + [{"role": "user", "content": user_text}]
    answer = call_openai_chat(messages, temperature=0.2)

    # Post-processing per link video/accesso/corrente
    lower = user_text.lower()
    property_name = (booking_ctx.get("property", {}) or {}).get("name")
    wants_access = any(x in lower for x in [
        "come entro", "come faccio a entrare", "self check", "self-check",
        "video", "accesso", "codice porta", "corrente", "check in", "check-in"
    ])

    # Spinta automatica se condizioni ok
    if property_name and should_offer_checkin_assets(booking_ctx):
        link = choose_video_link(property_name)
        if link and link not in answer:
            answer = (answer + f"\n\n🔑 Accesso per *{property_name}*: {link}").strip()
    # Su richiesta esplicita
    elif property_name and wants_access:
        link = choose_video_link(property_name)
        if link and link not in answer:
            answer = (answer + f"\n\n🔗 Video per *{property_name}*: {link}").strip()

    # salva history
    sess["history"] = hist + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": answer},
    ]
    session_store[phone] = sess
    return answer

# ---------------- Test UI ----------------
@app.route("/")
def root():
    return "OK"

@app.route("/test", methods=["GET"])
def test_page():
    return render_template("test.html")

@app.route("/test_api", methods=["POST"])
def test_api():
    phone = normalize_sender(request.form.get("phone") or "")
    text = (request.form.get("text") or "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "missing phone"}), 400
    if not text:
        return jsonify({"ok": False, "error": "missing text"}), 400

    logger.debug("TEST Inbound da %s: %s", phone, text)
    reply = make_assistant_reply(phone, text)
    return jsonify({"ok": True, "reply": reply})

# ---------------- Twilio webhook ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logger.debug("Inbound da %s: %s", sender, body)

    reply = make_assistant_reply(sender, body)
    twiml = MessagingResponse()
    twiml.message(reply)
    return str(twiml)

# ---------------- Run (locale) ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
