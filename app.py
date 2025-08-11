import os
import re
import time
import logging
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from ciao_booking_client import CiaoBookingClient

# ------------------------------------------------------------------------------
# Logging (accetta "debug", "INFO", ecc.)
# ------------------------------------------------------------------------------
_level_str = os.environ.get("LOG_LEVEL", "INFO")
_level_map = {
    "CRITICAL": logging.CRITICAL, "critical": logging.CRITICAL,
    "ERROR": logging.ERROR,       "error": logging.ERROR,
    "WARNING": logging.WARNING,   "warning": logging.WARNING,
    "INFO": logging.INFO,         "info": logging.INFO,
    "DEBUG": logging.DEBUG,       "debug": logging.DEBUG,
}
logging.basicConfig(
    level=_level_map.get(_level_str, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

# ------------------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------------------
app = Flask(__name__)

# ------------------------------------------------------------------------------
# OpenAI client (usa openai>=1.x)
# ------------------------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY non impostata: le risposte GPT non funzioneranno.")
openai_client = OpenAI(api_key=OPENAI_API_KEY)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # veloce/economico, cambia se vuoi

# ------------------------------------------------------------------------------
# CiaoBooking client
# ------------------------------------------------------------------------------
CIAO_BASE = os.environ.get("CIAOBOOKING_BASE_URL", "https://api.ciaobooking.com")
CIAO_TOKEN = os.environ.get("CIAOBOOKING_TOKEN")  # opzionale, se fornito salta il login
CIAO_EMAIL = os.environ.get("CIAOBOOKING_EMAIL")
CIAO_PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD")
CIAO_SOURCE = os.environ.get("CIAOBOOKING_SOURCE", "whatsapp-bot")
CIAO_LOCALE = os.environ.get("CIAOBOOKING_LOCALE", "it")

ciao = CiaoBookingClient(
    base_url=CIAO_BASE,
    static_token=CIAO_TOKEN,
    email=CIAO_EMAIL,
    password=CIAO_PASSWORD,
    source=CIAO_SOURCE,
    locale=CIAO_LOCALE,
)

# ------------------------------------------------------------------------------
# Stato in memoria (per singolo pod)
# ------------------------------------------------------------------------------
SESS = {}  # { sender: { "created_at":ts, "booking_ctx":{...}, "last_intent":..., ... } }
SESSION_TTL_SEC = 60 * 60 * 4  # 4 ore

def normalize_sender(raw):
    # Twilio manda "whatsapp:+39...." -> teniamo solo numero normalizzato (senza +)
    if not raw:
        return ""
    m = re.search(r"\+?(\d+)$", raw)
    return m.group(1) if m else raw.replace("whatsapp:", "").replace("+", "").strip()

def get_session(sender):
    now = time.time()
    s = SESS.get(sender)
    if not s or (now - s.get("created_at", 0)) > SESSION_TTL_SEC:
        s = {"created_at": now}
        SESS[sender] = s
    return s

def ensure_booking_context(sender):
    """
    Al primo messaggio della conversazione prova a riconoscere il cliente su CiaoBooking.
    Cache in memoria per la durata della sessione.
    """
    s = get_session(sender)
    if "booking_ctx" in s:
        return s["booking_ctx"]

    # lookup client by phone; se fallisce NON blocchiamo la conversazione
    try:
        ctx = ciao.get_booking_context_by_phone(sender)
        s["booking_ctx"] = ctx or {"has_client": False}
        if ctx and ctx.get("has_client"):
            logging.info("CiaoBooking client trovato: %s", ctx.get("client", {}).get("name"))
        else:
            logging.info("CiaoBooking client NON trovato per %s", sender)
    except Exception as e:
        logging.error("Errore lookup CiaoBooking: %s", e)
        s["booking_ctx"] = {"has_client": False, "error": "lookup_failed"}

    return s["booking_ctx"]

def ask_entry_question(has_client):
    """
    Prima domanda della conversazione: se c’è prenotazione, chiediamo scopo;
    se non c’è, chiediamo se ha una prenotazione o vuole info generiche.
    """
    if has_client:
        return ("Ciao! Come posso aiutarti? Scrivi *Taxi/Transfer*, *Parcheggio* "
                "o *Altro*. Se ti serve l’accesso ti mando i video/istruzioni.")
    else:
        return ("Ciao! Hai già una prenotazione con noi? Rispondi *Sì* o *No*.\n"
                "Oppure dimmi *Parcheggio*, *Taxi/Transfer* o *Altro*.")

# ------------------------------------------------------------------------------
# Prompt di sistema minimo (il grosso lo decide la logica; GPT è di supporto)
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Sei un assistente per una struttura ricettiva a Pisa. "
    "Sii chiaro, cordiale e conciso; fai domande mirate solo quando servono. "
    "Se l’utente chiede *Taxi/Transfer*, guida una breve raccolta dati: persone, orario, partenza/destinazione. "
    "Tariffe: Aeroporto ↔ Città 50€, Città ↔ Città 40€. "
    "Se chiede *Parcheggio*, chiedi prima in quale struttura si trova (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina) "
    "e rispondi con le info giuste. Evita di elencare informazioni non richieste."
)

def gpt_reply(user_msg, booking_ctx):
    """
    Fallback/intelligenza per formulare risposte naturali basate su regole minime.
    """
    try:
        msg = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
        )
        return msg.choices[0].message.content.strip()
    except Exception as e:
        logging.error("Errore OpenAI: %s", e)
        return "Mi dispiace, c’è stato un problema temporaneo. Riprova tra poco."

# ------------------------------------------------------------------------------
# Webhook Twilio
# ------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return "OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)

    logging.debug("Inbound da %s: %s", sender, body)

    # Reset conversazione utente
    if body.lower() in ("/reset", "reset"):
        SESS.pop(sender, None)
        tw = MessagingResponse()
        tw.message("✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Altro)")
        return str(tw)

    s = get_session(sender)
    ctx = ensure_booking_context(sender)  # { has_client:bool, client:{...}?, ... }

    # Stato conversazione
    last_intent = s.get("last_intent")

    # 1) Se non c'è ancora un intent, poniamo la domanda di ingresso adatta
    if not last_intent:
        # prova a capire l’intent dal testo subito
        lower = body.lower()
        if any(k in lower for k in ["taxi", "transfer", "trasfer", "aeroporto", "airport"]):
            s["last_intent"] = "taxi"
        elif "parche" in lower:
            s["last_intent"] = "parking"
        elif lower in ("si", "sì", "yes") and not ctx.get("has_client"):
            # utente dice “Sì ho prenotazione” ma ciao booking non conferma → chiediamo nome o cell
            tw = MessagingResponse()
            tw.message("Perfetto! Non trovo la prenotazione con questo numero. Puoi indicarmi *nome e cognome* usati in prenotazione o *il numero di telefono* associato?")
            return str(tw)
        elif lower in ("no", "non ancora", "no grazie"):
            tw = MessagingResponse()
            tw.message("Nessun problema. Posso aiutarti con *Taxi/Transfer*, *Parcheggio* o informazioni sulla *struttura*.")
            return str(tw)
        else:
            # domanda di ingresso coerente con presenza prenotazione
            tw = MessagingResponse()
            tw.message(ask_entry_question(ctx.get("has_client", False)))
            return str(tw)

    # 2) Intent TAXI
    if s.get("last_intent") == "taxi":
        # Raccogliamo in sessione i campi che mancano
        form = s.get("taxi_form", {"people": None, "time": None, "from": None, "to": None})
        lower = body.lower()

        # Heuristics leggere per estrarre info rapide dal testo
        # persone (numero)
        if form["people"] is None:
            m = re.search(r"\b([1-9]\d?)\b", body)
            if m:
                form["people"] = m.group(1)
            else:
                tw = MessagingResponse()
                tw.message("Quante persone siete?")
                s["taxi_form"] = form
                return str(tw)

        # orario
        if form["time"] is None:
            m = re.search(r"\b(\d{1,2}[:.]\d{2})\b", body)
            if m:
                form["time"] = m.group(1).replace(".", ":")
            else:
                tw = MessagingResponse()
                tw.message("A che ora desideri la partenza? (es. 14:30)")
                s["taxi_form"] = form
                return str(tw)

        # partenza/destinazione: se nel testo iniziale c'era “aeroporto” deduciamo
        if form["from"] is None or form["to"] is None:
            if any(k in lower for k in ["aeroporto", "airport"]):
                # Se ha nominato aeroporto senza dire altro, chiedi l’altro estremo
                if form["from"] is None and form["to"] is None:
                    # prova a capire direzione: “dall’aeroporto” vs “all’aeroporto”
                    if "dall" in lower or "da aeroporto" in lower:
                        form["from"] = "Aeroporto"
                    elif "all" in lower or "verso aeroporto" in lower:
                        form["to"] = "Aeroporto"
            # se ancora manca qualcosa, chiedi in modo esplicito
            if form["from"] is None:
                tw = MessagingResponse()
                tw.message("Qual è il *punto di partenza*? (es. Aeroporto o nome della struttura)")
                s["taxi_form"] = form
                return str(tw)
            if form["to"] is None:
                tw = MessagingResponse()
                tw.message("Qual è la *destinazione*? (es. Relais dell’Ussero, Casa Monic, Belle Vue, ecc.)")
                s["taxi_form"] = form
                return str(tw)

        # a questo punto abbiamo tutti i campi → calcolo tariffa
        start = (form["from"] or "").lower()
        end = (form["to"] or "").lower()
        if "aeroporto" in start or "aeroporto" in end or "airport" in start or "airport" in end:
            price = "50€"
        else:
            price = "40€"

        summary = (
            "Perfetto, riepilogo:\n"
            f"• Persone: {form['people']}\n"
            f"• Orario: {form['time']}\n"
            f"• Partenza: {form['from']}\n"
            f"• Destinazione: {form['to']}\n\n"
            f"Tariffa: {price}.\n"
            "Confermi che va bene? (sì/no)\n"
            "Se confermi, Niccolò ti contatterà a breve per la conferma definitiva."
        )
        s["taxi_form"] = form
        s["awaiting_taxi_confirm"] = True
        tw = MessagingResponse()
        tw.message(summary)
        return str(tw)

    # 3) Conferma taxi
    if s.get("awaiting_taxi_confirm"):
        if body.strip().lower() in ("si", "sì", "yes", "ok", "va bene", "confermo"):
            s.pop("awaiting_taxi_confirm", None)
            s["last_intent"] = None
            tw = MessagingResponse()
            tw.message("Perfetto 👍 Ho memorizzato la richiesta. Niccolò ti contatterà a breve per confermare il transfer.")
            return str(tw)
        elif body.strip().lower() in ("no", "annulla", "cancella"):
            s.pop("awaiting_taxi_confirm", None)
            s["last_intent"] = None
            tw = MessagingResponse()
            tw.message("Ok, richiesta annullata. Se ti serve altro sono qui.")
            return str(tw)
        else:
            tw = MessagingResponse()
            tw.message("Puoi dirmi *sì* per confermare o *no* per annullare?")
            return str(tw)

    # 4) Intent PARCHEGGIO (semplice instradamento: chiedi struttura se manca)
    if s.get("last_intent") == "parking":
        # Se l’utente non ha specificato la struttura, chiedila
        if not s.get("structure"):
            lower = body.lower()
            known = {
                "relais": "Relais dell’Ussero",
                "ussero": "Relais dell’Ussero",
                "monic": "Casa Monic",
                "belle": "Belle Vue",
                "vue": "Belle Vue",
                "villino": "Villino di Monic",
                "gina": "Casa di Gina",
            }
            chosen = None
            for k, name in known.items():
                if k in lower:
                    chosen = name
                    break
            if not chosen:
                tw = MessagingResponse()
                tw.message("Per il parcheggio, in quale struttura stai alloggiando? (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)")
                return str(tw)
            s["structure"] = chosen

        # risposte parcheggio per struttura
        struct = s["structure"]
        if struct == "Casa Monic":
            msg = "Casa Monic: parcheggio pubblico in *Piazza Carrara* o *Piazza Santa Caterina* (~400m), €1,50/h."
        elif struct == "Belle Vue":
            msg = ("Belle Vue: sotto al palazzo in *Via Antonio Rosmini* o in *Via Pasquale Galluppi* "
                   "(a pagamento 08:00–14:00, poi gratis). Parcheggio custodito H24 in *Via Piave* (a pagamento).")
        elif struct == "Relais dell’Ussero":
            msg = "Relais dell’Ussero: parcheggio pubblico *Piazza Carrara* (pochi metri), €1,50/h."
        elif struct == "Casa di Gina":
            msg = ("Casa di Gina: *Via Crispi*, *Piazza Aurelio Saffi* o *Lungarno Sidney Sonnino*, "
                   "€1,50/h.")
        elif struct == "Villino di Monic":
            msg = "Villino di Monic: posteggio privato *non* indicato in KB; ti metto in contatto con Niccolò."
        else:
            msg = "Per questa struttura non ho indicazioni parcheggio in KB; ti metto in contatto con Niccolò."
        tw = MessagingResponse()
        tw.message(msg)
        # chiudiamo intent
        s["last_intent"] = None
        return str(tw)

    # 5) Altre richieste → fallback GPT
    reply = gpt_reply(body, ctx)
    tw = MessagingResponse()
    tw.message(reply)
    return str(tw)

from flask import Flask, request, jsonify, send_from_directory
import logging
import os
# ... altri import esistenti

app = Flask(__name__)

# --- configurazioni logging e variabili ---
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# --- eventuali funzioni di supporto ---
# def get_session(...):
# def ensure_booking_context(...):
# ecc...

# --- qui ci sono già le tue rotte WhatsApp /webhook ---
@app.route("/webhook", methods=["POST"])
def webhook():
    # logica esistente per messaggi da Twilio
    pass

# --- QUI AGGIUNGI LE DUE NUOVE ROTTE ---
@app.route("/test")
def test_page():
    return send_from_directory(".", "test_client.html")

@app.route("/test_api", methods=["POST"])
def test_api():
    data = request.get_json(force=True)
    sender = data.get("sender", "test")
    body = data.get("message", "").strip()

    s = get_session(sender)
    ctx = ensure_booking_context(sender)
    last_intent = s.get("last_intent")

    # Qui puoi richiamare la stessa logica di webhook() o fare un reply diretto
    reply = gpt_reply(body, ctx)
    return jsonify({"reply": reply})

# --- avvio applicazione ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

