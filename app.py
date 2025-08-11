
import os
import json
import logging
from flask import Flask, request, jsonify, render_template
from twilio.twiml.messaging_response import MessagingResponse

from state import STATE
from nlp import detect_intent, extract_slots, STRUCTURE_ALIASES
from ciao_booking_client import find_client_by_phone

# ---- Logging safe config ----
_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _level not in ("DEBUG","INFO","WARNING","ERROR","CRITICAL"):
    _level = "INFO"
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")

# ---- Load KB ----
with open(os.path.join(os.path.dirname(__file__), "kb", "knowledge_base.json"), "r", encoding="utf-8") as f:
    KB = json.load(f)

SENSITIVE = [s.lower() for s in KB["global_rules"]["sensitive_keywords"]]
SENSITIVE_REPLY = KB["global_rules"]["sensitive_response"]

app = Flask(__name__)

@app.get("/")
def index():
    return "OK"

def _contains_sensitive(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in SENSITIVE)

def _resolve_structure(current_struct: str, slots: dict, booking_ctx: dict):
    if current_struct:
        return current_struct
    if slots.get("structure"):
        return slots["structure"]
    if booking_ctx and booking_ctx.get("name"):
        # In futuro mappare alla struttura esatta dalla prenotazione
        return None
    return None

def _parking_answer(structure: str) -> str:
    sdata = KB["structures"].get(structure)
    if not sdata:
        return "Per il parcheggio ti aiuto volentieri: in quale struttura stai soggiornando?"
    return f"Parcheggio per *{structure}*: {sdata.get('parking','Informazione non disponibile.')}."

def _power_answer(structure: str) -> str:
    if structure == "Casa Monic":
        info = KB["structures"]["Casa Monic"].get("power")
        vid = KB["structures"]["Casa Monic"]["videos"].get("power_restore")
        return f"{info}\nVideo: {vid}"
    elif structure in KB["structures"]:
        return "Per problemi di corrente in questa struttura, ti metto in contatto con Niccolò."
    else:
        return "Mi indichi la struttura? Così verifico come aiutarti per la corrente."

def _transfer_collect(state: dict, slots: dict, text: str) -> (dict, str, bool):
    # Update slots
    for k,v in slots.items():
        state["transfer"][k] = v
    missing = []
    if "origin" not in state["transfer"]:
        # infer simple heuristics
        if "aeroporto" in text.lower():
            state["transfer"]["origin"] = "aeroporto"
        else:
            missing.append("punto di partenza")
    if "destination" not in state["transfer"]:
        if any(name.lower() in text.lower() for name in STRUCTURE_ALIASES.keys()):
            # nlp.extract already sets structure if matched
            if "structure" in state["transfer"]:
                state["transfer"]["destination"] = state["transfer"]["structure"]
        else:
            missing.append("destinazione")
    if "people" not in state["transfer"]:
        missing.append("quante persone")
    if "time" not in state["transfer"]:
        missing.append("orario di partenza")

    if missing:
        return state, f"Per organizzare il transfer, mi indichi: {', '.join(missing)}?", False

    # All gathered -> price
    o = state["transfer"].get("origin","").lower()
    d = state["transfer"].get("destination","").lower()
    airport = ("aeroporto" in o) or ("aeroporto" in d) or ("airport" in o) or ("airport" in d)
    price = KB["transfer_policy"]["ncc"]["aeroporto<->città"] if airport else KB["transfer_policy"]["ncc"]["città<->città"]
    summary = (
        "Perfetto, riepilogo:\n"
        f"• Persone: {state['transfer']['people']}\n"
        f"• Orario: {state['transfer']['time']}\n"
        f"• Partenza: {state['transfer'].get('origin','')}\n"
        f"• Destinazione: {state['transfer'].get('destination','')}\n"
        f"Tariffa: {price}€\n"
        "Confermi che la tariffa va bene? (sì/no)"
    )
    state["transfer"]["_price"] = price
    state["transfer"]["_await_confirm"] = True
    return state, summary, True

def _handle_message(phone: str, text: str) -> str:
    # Reset
    if text.strip().lower() == "/reset":
        STATE.reset(phone)
        return "✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Altro)"

    # Load state
    s = STATE.get(phone) or {"asked_intro": False, "structure": None, "booking_checked": False}

    # First message: run one-time CiaoBooking lookup
    if not s.get("booking_checked"):
        try:
            client = find_client_by_phone(phone)
        except Exception:
            client = None
        s["booking_checked"] = True
        if client:
            s["client"] = {"id": client.get("id"), "name": client.get("name")}
        STATE.set(phone, s)

    # Sensitive info guard
    if _contains_sensitive(text):
        return SENSITIVE_REPLY

    # Intro if first turn
    if not s.get("asked_intro"):
        s["asked_intro"] = True
        STATE.set(phone, s)
        # Ask purpose and (only if not found booking) ask if has reservation
        if not s.get("client"):
            return "Ciao! Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Altro*? Hai già una prenotazione a tuo nome?"
        else:
            return "Ciao! Come posso aiutarti oggi? *Taxi/Transfer*, *Parcheggio* o *Altro*?"

    # Intent & slots
    intent = detect_intent(text)
    slots = extract_slots(text)

    # If user states structure, bind it
    if slots.get("structure"):
        s["structure"] = slots["structure"]
        STATE.set(phone, s)

    # Transfer flow
    if intent == "transfer" or s.get("transfer"):
        s.setdefault("transfer", {})
        # If user gave destination 'aeroporto' or mentions airport, set origin/dest heuristics
        state, msg, maybe_done = _transfer_collect(s, slots, text)
        STATE.set(phone, state)
        if maybe_done and state["transfer"].get("_await_confirm") and text.strip().lower() in ("si","sì","ok","va bene","confermo","yes","y"):
            price = state["transfer"]["_price"]
            state["transfer"]["_await_confirm"] = False
            STATE.set(phone, state)
            return f"👍 Perfetto! Ho registrato la richiesta (tariffa {price}€). Niccolò ti contatterà a breve per la conferma definitiva."
        # If user says no
        if maybe_done and state["transfer"].get("_await_confirm") and text.strip().lower() in ("no","non confermo","annulla"):
            state["transfer"] = {}
            STATE.set(phone, state)
            return "Ok, annullato. Posso aiutarti con altro (Parcheggio, Corrente, Check-in)?"
        return msg

    # Parking
    if intent == "parking":
        struct = _resolve_structure(s.get("structure"), slots, s.get("client"))
        if not struct:
            return "Per il parcheggio mi dici in quale struttura alloggi? (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)"
        return _parking_answer(struct)

    # Power
    if intent == "power":
        struct = _resolve_structure(s.get("structure"), slots, s.get("client"))
        if not struct:
            return "Mi indichi la struttura? Così ti aiuto al meglio per la corrente."
        return _power_answer(struct)

    # Check-in info
    if intent == "checkin_info":
        struct = _resolve_structure(s.get("structure"), slots, s.get("client"))
        if not struct:
            return "Per inviarti il video di check-in, mi dici la struttura?"
        vids = KB["structures"].get(struct, {}).get("videos", {})
        if "self_checkin" in vids:
            return f"Video check-in *{struct}*: {vids['self_checkin']}"
        return "Al momento non ho un video per questa struttura. Per dettagli operativi ti scriverà Niccolò."

    # Greetings / other
    if intent == "greeting":
        return "Dimmi pure: *Taxi/Transfer*, *Parcheggio* o *Altro*?"
    return "Ok! Se ti serve *Taxi/Transfer* o *Parcheggio* dillo pure; altrimenti chiedimi quello che ti serve (es. video check-in)."

@app.post("/webhook")
def webhook():
    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "")
    logging.debug("Inbound from %s: %s", from_number, body)

    reply = _handle_message(from_number or "anon", body or "")
    tw = MessagingResponse()
    tw.message(reply)
    xml = str(tw)
    logging.debug("Responding to %s with TwiML: %s", from_number, xml)
    return xml

# ---- Test UI (no Twilio) ----
@app.get("/test")
def test_ui():
    return render_template("test.html")

@app.post("/test/send")
def test_send():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "tester")
    text = data.get("text", "")
    reply = _handle_message(phone, text)
    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
