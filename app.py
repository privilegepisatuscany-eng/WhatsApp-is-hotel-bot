import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from twilio.twiml.messaging_response import MessagingResponse

import httpx
from openai import OpenAI

from ciao_booking_client import CiaoBookingClient
from utils import normalize_sender, clamp_history

# -------- Logging robusto --------
_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
    _level = "INFO"
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# -------- Flask --------
app = Flask(__name__)

# -------- OpenAI client (fix: niente proxies=; supporto proxy via httpx) --------
def make_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY mancante nelle env vars")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        http_client = httpx.Client(proxies=proxy, timeout=30.0, follow_redirects=True)
        return OpenAI(api_key=api_key, http_client=http_client)
    return OpenAI(api_key=api_key)

client = make_openai_client()

# -------- Knowledge Base --------
with open("knowledge_base.json", "r", encoding="utf-8") as f:
    KB = json.load(f)

# -------- CiaoBooking --------
CB = CiaoBookingClient(
    base_url="https://api.ciaobooking.com",
    email=os.environ.get("CIAOBOOKING_EMAIL", ""),
    password=os.environ.get("CIAOBOOKING_PASSWORD", ""),
    locale=os.environ.get("CIAOBOOKING_LOCALE", "it"),
)

# -------- Memoria conversazionale in RAM --------
session_store = {}

SYSTEM_INSTRUCTIONS = (
    "Sei un assistente per strutture ricettive a Pisa. "
    "Parla in modo chiaro, cortese e conciso. "
    "Usa SOLO le informazioni presenti nella knowledge base (KB) oppure nel contesto prenotazione, senza inventare. "
    "Non richiedere due volte le stesse informazioni se sono già note nel contesto. "
    "Se l’utente chiede Transfer/Taxi: estrai persone, orario, partenza e destinazione dal messaggio; "
    "applica le tariffe KB (Aeroporto↔Città 50€, Città↔Città 40€). "
    "Se l’utente chiede Parcheggio: usa la struttura del cliente (se nota) altrimenti chiedi in quale struttura alloggia. "
    "Se chiede Corrente/Ripristino: fornisci le istruzioni dalla KB per la struttura specifica; non proporre spontaneamente problemi di corrente. "
    "Se in KB è presente un video per la struttura/scopo richiesto, includi il link. "
    "Se la prenotazione non è stata trovata, continua comunque ad aiutare usando la KB generale. "
    "Non ripetere il saluto iniziale a ogni messaggio; prosegui dal contesto. "
)

def build_kb_blob():
    """Appiattisce la KB in una stringa corta ma completa per il prompt."""
    parts = []

    # Transfer/Taxi
    tx = KB.get("transfer", {})
    parts.append(
        f"[TRANSFER]\n"
        f"Aeroporto↔Città: {tx.get('price_airport_city','50€')} - Città↔Città: {tx.get('price_city_city','40€')}\n"
        f"Dati richiesti: {', '.join(tx.get('required_fields',['persone','orario','partenza','destinazione']))}\n"
        f"Pagamento: {tx.get('payment','contanti o carta')}.\n"
    )

    # Parcheggi per struttura
    parks = KB.get("parking", {})
    parts.append("[PARCHEGGIO]")
    for prop, info in parks.items():
        parts.append(f"- {prop}: {info}")

    # Video per struttura (check-in / corrente)
    vids = KB.get("videos", {})
    parts.append("[VIDEO]")
    for prop, m in vids.items():
        ci = m.get("checkin")
        ce = m.get("current") or m.get("corrente")
        line = f"- {prop}: "
        if ci: line += f"check-in: {ci} "
        if ce: line += f"| corrente: {ce}"
        parts.append(line.strip())

    # Istruzioni ripristino corrente specifiche
    power = KB.get("power", {})
    parts.append("[CORRENTE]")
    for prop, instr in power.items():
        parts.append(f"- {prop}: {instr}")

    # Orari generali
    hours = KB.get("hours", {})
    parts.append(
        "[ORARI]\n"
        f"Check-in: {hours.get('checkin','15:00 - 04:00')}; Check-out: {hours.get('checkout','10:00')}."
    )

    # Contatti
    contacts = KB.get("contacts", {})
    parts.append(
        "[CONTATTI]\n"
        f"Host: Monica {contacts.get('monica','')}, Niccolò {contacts.get('niccolo','')}"
    )

    return "\n".join(parts)

KB_BLOB = build_kb_blob()

def build_booking_blob(ctx: dict | None) -> str:
    if not ctx:
        return "Prenotazione: non trovata."
    txt = ["[PRENOTAZIONE]"]
    if ctx.get("client_name"):
        txt.append(f"Cliente: {ctx['client_name']}")
    if ctx.get("property"):
        txt.append(f"Struttura: {ctx['property']}")
    if ctx.get("start_date") and ctx.get("end_date"):
        txt.append(f"Soggiorno: {ctx['start_date']} → {ctx['end_date']}")
    if ctx.get("docs_status"):
        txt.append(f"Documenti: {ctx['docs_status']}")
    if ctx.get("reservation_id"):
        txt.append(f"ReservationID: {ctx['reservation_id']}")
    return "\n".join(txt)

def ensure_session(sender: str):
    if sender not in session_store:
        session_store[sender] = {
            "history": [],
            "booking_ctx": None,
            "created_at": datetime.utcnow().isoformat()
        }

def gpt_reply(history: list[dict], booking_ctx: dict | None) -> str:
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    messages.append({"role": "system", "content": KB_BLOB})
    messages.append({"role": "system", "content": build_booking_blob(booking_ctx)})

    # includo solo ultimi 6 scambi per non gonfiare
    trimmed = history[-12:]
    messages.extend(trimmed)

    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.3,
        messages=messages,
    )
    return resp.choices[0].message.content.strip()

def first_touch_booking_lookup(sender: str):
    """Primo messaggio della conversazione: tenta il lookup CiaoBooking una sola volta."""
    s = session_store[sender]
    if s.get("booking_ctx_checked"):
        return
    try:
        ctx = CB.get_booking_context_by_phone(sender)
        if ctx:
            s["booking_ctx"] = ctx
            logger.info("CiaoBooking: contesto prenotazione trovato per %s", sender)
        else:
            logger.info("CiaoBooking: client non trovato (%s)", sender)
    except Exception as e:
        logger.error("CiaoBooking lookup errore: %s", e)
    finally:
        s["booking_ctx_checked"] = True

def process_message(sender: str, text: str) -> str:
    ensure_session(sender)
    s = session_store[sender]

    # primo messaggio → lookup prenotazione
    if not s.get("history"):
        first_touch_booking_lookup(sender)

    # aggiorna history
    s["history"].append({"role": "user", "content": text})
    s["history"] = clamp_history(s["history"], max_turns=12)

    # risposta LLM
    answer = gpt_reply(s["history"], s.get("booking_ctx"))
    s["history"].append({"role": "assistant", "content": answer})
    s["history"] = clamp_history(s["history"], max_turns=12)

    return answer

# ---------------- Routes ----------------

@app.route("/", methods=["GET"])
def index():
    return "OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    """Twilio webhook e anche endpoint usato dal tester."""
    sender_raw = request.form.get("From", "") or request.args.get("phone", "")
    body = (request.form.get("Body") or request.form.get("text") or "").strip()
    sender = normalize_sender(sender_raw)

    logger.debug("Inbound da %s: %s", sender, body)
    if not body:
        tw = MessagingResponse()
        tw.message("Dimmi pure come posso aiutarti.")
        return str(tw)

    if body.strip().lower() == "/reset":
        session_store.pop(sender, None)
        tw = MessagingResponse()
        tw.message("✅ Conversazione resettata. Come posso aiutarti?")
        return str(tw)

    try:
        reply = process_message(sender, body)
    except Exception as e:
        logger.exception("Errore processamento messaggio")
        reply = "Mi dispiace, ho avuto un problema temporaneo. Riprova tra poco."

    # Se arriva da Twilio: restituisci TwiML
    if request.form.get("From"):
        tw = MessagingResponse()
        tw.message(reply)
        return str(tw)

    # Se arriva dal tester: JSON
    return jsonify({"reply": reply})

# Pagina test con storico e telefono modificabile
@app.route("/test", methods=["GET"])
def test_page():
    return render_template("test.html")

@app.route("/test_api", methods=["POST"])
def test_api():
    data = request.get_json(force=True)
    phone = normalize_sender(data.get("phone", ""))
    text = (data.get("text") or "").strip()
    if not phone or not text:
        return jsonify({"error": "phone e text sono obbligatori"}), 400

    try:
        reply = process_message(phone, text)
        history = session_store.get(phone, {}).get("history", [])
        return jsonify({"reply": reply, "history": history})
    except Exception as e:
        logger.exception("Errore /test_api")
        return jsonify({"error": "internal error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
