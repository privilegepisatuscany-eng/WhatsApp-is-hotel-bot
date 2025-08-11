import os
import json
import logging
from flask import Flask, request, render_template, jsonify
from twilio.twiml.messaging_response import MessagingResponse

from state import MemoryStore
from nlp import detect_intent, normalize_sender, fmt_euro
from ciao_booking_client import (
    cb_login,
    find_client_by_phone,
    find_reservation_by_ref,
)
# Carica KB
with open(os.path.join("kb", "knowledge_base.json"), "r", encoding="utf-8") as f:
    KB = json.load(f)

# Logging robusto: accetta "debug"/"INFO"/etc.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
    LOG_LEVEL = "INFO"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)
state = MemoryStore()

# --- Helper risposta WhatsApp/Test ---
def twiml(text):
    tw = MessagingResponse()
    tw.message(text)
    return str(tw)

def answer_text(text):
    # per /test_api (ritorna semplice stringa)
    return text

# --- Intent Handlers ---

def handle_greeting(ctx, kb):
    """
    Primo messaggio: se non abbiamo ancora il contesto CiaoBooking per il caller,
    proviamo lookup automatico (in base al numero) UNA sola volta.
    Poi chiediamo subito lo scopo (Taxi/Transfer, Parcheggio, Video/Accesso),
    e SE non troviamo il cliente chiediamo nome o ref se serve.
    """
    greeting = "Ciao! Come posso aiutarti oggi?"
    # Lookup automatico una volta per sessione
    if not ctx.get("cb_checked"):
        ctx["cb_checked"] = True
        token = cb_login()
        if token:
            client = find_client_by_phone(ctx["phone"], token)
            if client:
                ctx["client"] = client
                ctx["client_known"] = True
                logging.info("CiaoBooking: client trovato")
            else:
                ctx["client_known"] = False
                logging.info("CiaoBooking: client non trovato (%s)", ctx["phone"])
        else:
            logging.warning("CiaoBooking login fallito; procedo senza contesto")
    # Prompt breve, niente reset conversazione
    follow = "Posso aiutarti con *Taxi/Transfer*, *Parcheggio* o *Video/Accesso*."
    # Se non riconosciuto il cliente, **non** chiediamo più numeri di telefono,
    # ma se l’utente scrive esplicitamente “ho prenotazione” poi raccoglieremo ref nell’intent.
    return f"{greeting}\n{follow}"

def handle_transfer(ctx, kb, msg):
    """
    Flusso transfer: 2 regole semplici
    - Se il testo indica aeroporto ↔ città, tariffa 50€.
    - Altrimenti 40€.
    Raccogliamo: persone, orario, partenza/destinazione (solo quelli mancanti).
    Poi **chiediamo conferma tariffa** e chiudiamo.
    """
    form = ctx.setdefault("transfer_form", {"people": None, "time": None, "from": None, "to": None})
    lower = msg.lower()

    # Pre‑estrazione semplice
    import re
    if form["people"] is None:
        m = re.search(r"\b(\d+)\s*(persone|ospiti|pax)?\b", lower)
        if m:
            form["people"] = int(m.group(1))
    if form["time"] is None:
        m = re.search(r"\b(\d{1,2}[:\.]\d{2})\b", lower)
        if m:
            form["time"] = m.group(1).replace(".", ":")

    # Partenza/destinazione
    if form["from"] is None:
        if "aeroporto" in lower:
            form["from"] = "Aeroporto"
        elif "stazione" in lower:
            form["from"] = "Stazione"
    if form["to"] is None:
        # nomi strutture note
        for name in kb["structures"].keys():
            if name.lower() in lower:
                form["to"] = name
                break

    # Se l’utente ha scritto esplicitamente “dall’aeroporto a X”
    if "dall'aeroporto" in lower or "dall aeroporto" in lower:
        form["from"] = "Aeroporto"
    if "allo" in lower or "a " in lower:
        # già coperto sopra dal match struttura, lasciamo così per semplicità
        pass

    # Chiedi solo i campi mancanti, con ordine smart
    if form["people"] is None:
        return "Quante persone siete per il transfer?"
    if form["time"] is None:
        return "A che ora desideri la partenza?"
    if form["from"] is None:
        return "Da dove partite? (Aeroporto/Stazione/Altro)"
    if form["to"] is None:
        return "Qual è la destinazione? (puoi indicare la struttura)"

    # Tutti i dati: calcolo tariffa
    def is_airport_leg(a, b):
        a = (a or "").lower()
        b = (b or "").lower()
        return "aeroporto" in a or "aeroporto" in b

    price = 50 if is_airport_leg(form["from"], form["to"]) else 40
    ctx["transfer_price"] = price

    summary = (
        "Perfetto, ho raccolto questi dati:\n"
        f"• Persone: {form['people']}\n"
        f"• Orario: {form['time']}\n"
        f"• Partenza: {form['from']}\n"
        f"• Destinazione: {form['to']}\n\n"
        f"Tariffa: {fmt_euro(price)}.\n"
        "Confermi che la tariffa ti sta bene? (sì/no)"
    )
    ctx["pending"] = "transfer_confirm"
    return summary

def handle_transfer_confirm(ctx, msg):
    if msg.strip().lower() in ("si", "sì", "ok", "va bene", "confermo"):
        ctx.pop("pending", None)
        return "👍 Perfetto, ho memorizzato la conferma. Niccolò ti contatterà a breve per i dettagli finali."
    elif msg.strip().lower() in ("no", "non va bene"):
        ctx.pop("pending", None)
        return "Ok, allora non procedo. Se vuoi modificare orario o tratta, dimmelo pure."
    else:
        return "Per favore rispondi *sì* o *no* alla conferma della tariffa."

def handle_parking(ctx, kb, msg):
    """
    Parcheggio: chiedi la struttura se non nota; poi rispondi solo con quelle info.
    """
    structure = ctx.get("structure")
    if not structure:
        # Prova estrazione rapida dal messaggio
        for name in kb["structures"].keys():
            if name.lower() in msg.lower():
                structure = name
                ctx["structure"] = name
                break
    if not structure:
        # chiedi
        names = ", ".join(kb["structures"].keys())
        return f"Per il parcheggio, in quale struttura ti trovi? ({names})"

    info = kb["structures"].get(structure, {})
    parking = info.get("parking")
    if not parking:
        return "Per questa struttura non ho dettagli di parcheggio aggiornati. Ti farò contattare da Niccolò."
    return parking

def handle_videos(ctx, kb, msg):
    """
    Mostra solo i video pertinenti alla struttura richiesta (o chiedi la struttura).
    Include video check-in e video ripristino corrente se presenti.
    """
    structure = ctx.get("structure")
    for name in kb["structures"].keys():
        if name.lower() in msg.lower():
            structure = name
            ctx["structure"] = name
            break

    if not structure:
        names = ", ".join(kb["structures"].keys())
        return f"Per i video, dimmi la struttura: ({names})"

    s = kb["structures"].get(structure, {})
    vids = s.get("videos", {})
    chunks = []
    if vids.get("checkin"):
        chunks.append(f"🎥 Video check‑in: {vids['checkin']}")
    if vids.get("power"):
        chunks.append(f"🔌 Ripristino corrente: {vids['power']}")
    if not chunks:
        return "Per questa struttura non ho video associati al momento."
    return "\n".join(chunks)

def handle_power(ctx, kb, msg):
    """
    Se chiede corrente, prova a legare alla struttura.
    Se è Casa Monic, inserisci anche il video.
    """
    structure = ctx.get("structure")
    for name in kb["structures"].keys():
        if name.lower() in msg.lower():
            structure = name
            ctx["structure"] = name
            break

    if not structure:
        names = ", ".join(kb["structures"].keys())
        return f"Per aiutarti sul ripristino corrente, dimmi la struttura: ({names})"

    s = kb["structures"].get(structure, {})
    tips = s.get("power_tips")
    video = s.get("videos", {}).get("power")
    if tips and video:
        return f"{tips}\n\n🎥 Video: {video}"
    if tips:
        return tips
    return "Per questa struttura non ho istruzioni sul ripristino corrente. Ti faccio contattare da Niccolò."

# --- Router principale ---
def route_message(ctx, kb, msg):
    # Pending step? (es. conferma transfer)
    if ctx.get("pending") == "transfer_confirm":
        return handle_transfer_confirm(ctx, msg)

    intent = detect_intent(msg)

    if intent == "transfer":
        ctx["last_intent"] = "transfer"
        return handle_transfer(ctx, kb, msg)

    if intent == "parking":
        ctx["last_intent"] = "parking"
        return handle_parking(ctx, kb, msg)

    if intent == "video":
        ctx["last_intent"] = "video"
        return handle_videos(ctx, kb, msg)

    if intent == "power":
        ctx["last_intent"] = "power"
        return handle_power(ctx, kb, msg)

    # Se riconosce un numero prenotazione, prova lookup (non obbligatorio per transfer/taxi)
    import re
    m = re.search(r"\b(\d{6,})\b", msg)
    if m:
        token = cb_login()
        if token:
            res = find_reservation_by_ref(m.group(1), token)
            if res:
                ctx["reservation_ref"] = m.group(1)
                # Se la prenotazione ha unit/property note, puoi salvarle in ctx["structure"] se mappabili
                return "Ok, ho collegato la tua prenotazione. Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Video/Accesso*."
        # se non trovata, non bloccare: si continua normalmente

    # fallback: greeting
    return handle_greeting(ctx, kb)

# --- Flask routes ---

@app.route("/", methods=["GET"])
def root():
    return "OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logging.debug("Inbound da %s: %s", sender, body)

    # stato conversazione
    ctx = state.get(sender)
    if not ctx:
        ctx = {"phone": sender.replace("whatsapp:", "").replace("+", "").strip()}
        state.set(sender, ctx)

    reply = route_message(ctx, KB, body)

    # Se la richiesta arriva da Twilio, rispondiamo con TwiML
    if "whatsapp:" in sender_raw or request.form.get("MessageSid"):
        return twiml(reply)
    # Per sicurezza (non dovrebbe capitare)
    return reply

# --- Test UI ---
@app.route("/test", methods=["GET"])
def test_page():
    return render_template("test.html")

@app.route("/test_api", methods=["POST"])
def test_api():
    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    if not phone or not message:
        return jsonify({"ok": False, "reply": "Telefono e messaggio sono obbligatori"}), 400

    sender = phone
    ctx = state.get(sender)
    if not ctx:
        ctx = {"phone": phone}
        state.set(sender, ctx)

    reply = route_message(ctx, KB, message)
    return jsonify({"ok": True, "reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
