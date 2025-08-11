import os, re, json, logging, time
from typing import Any, Dict, Optional
from flask import Flask, request, Response, render_template, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

from ciao_booking_client import (
    first_touch_ciaobooking_lookup,
    get_reservation_by_id,
)

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in ["CRITICAL","ERROR","WARNING","INFO","DEBUG","NOTSET"]:
    LOG_LEVEL = "INFO"
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

# ──────────────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")

# ──────────────────────────────────────────────────────────────────────────────
# OpenAI
# ──────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY non impostata")
client_ai = OpenAI(api_key=OPENAI_API_KEY)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# ──────────────────────────────────────────────────────────────────────────────
# In-memory session (per Render/PoC; in produzione usare Redis)
# ──────────────────────────────────────────────────────────────────────────────
SESS: Dict[str, Dict[str, Any]] = {}

def get_session(sender: str) -> Dict[str, Any]:
    s = SESS.setdefault(sender, {})
    s.setdefault("created_at", time.time())
    s.setdefault("flow", None)
    s.setdefault("last_intent_corrente", False)
    s.setdefault("history", [])
    return s

# ──────────────────────────────────────────────────────────────────────────────
# Knowledge base
# ──────────────────────────────────────────────────────────────────────────────
def load_kb() -> Dict[str, Any]:
    try:
        with open("knowledge_base.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning("Impossibile caricare knowledge_base.json: %s", e)
        return {}

KB: Dict[str, Any] = load_kb()

FALLBACK_VIDEOS = {
    "Relais dell’Ussero": "https://youtube.com/shorts/XnBcl2T-ewM?feature=share",
    "Casa Monic": "https://youtube.com/shorts/YHX-7uT3itQ?feature=share",
    "Belle Vue": "https://youtube.com/shorts/1iqknGhIFEc?feature=share",
    "Casa di Gina": "https://youtube.com/shorts/Wi-mevoKB3w?feature=share",
    "Casa Monic (ripristino corrente)": "https://youtube.com/shorts/UIozKt4ZrCk?feature=share",
}

def kb_get(path: str, default=None):
    try:
        cur = KB
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur
    except Exception:
        return default

def kb_video_for(structure: str) -> Optional[str]:
    videos = kb_get("videos", {}) or {}
    if not videos:
        videos = FALLBACK_VIDEOS

    s = structure.lower().strip()
    aliases = {
        "relais": "Relais dell’Ussero",
        "ussero": "Relais dell’Ussero",
        "casa monic": "Casa Monic",
        "casa di monic": "Casa Monic",
        "monic": "Casa Monic",
        "belle vue": "Belle Vue",
        "rosmini": "Belle Vue",
        "gina": "Casa di Gina",
        "casa di gina": "Casa di Gina",
        "villino": "Villino di Monic",
        "villino di monic": "Villino di Monic",
    }
    target = aliases.get(s)
    if not target:
        for key in list(videos.keys()) + list(FALLBACK_VIDEOS.keys()):
            if key.lower() in s or s in key.lower():
                target = key
                break
    if not target:
        target = structure
    return videos.get(target) or FALLBACK_VIDEOS.get(target)

# ──────────────────────────────────────────────────────────────────────────────
# Utils
# ──────────────────────────────────────────────────────────────────────────────
def normalize_sender(raw: str) -> str:
    # whatsapp:+39xxx or free text in tester
    raw = raw.strip()
    if raw.startswith("whatsapp:"):
        raw = raw.split("whatsapp:")[-1]
    raw = raw.replace("+", "").replace(" ", "")
    return raw

def twiml_message(text: str) -> str:
    tw = MessagingResponse()
    tw.message(text)
    return str(tw)

def ai_reply(system: str, user: str) -> str:
    try:
        resp = client_ai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.3,
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":user}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error("AI error: %s", e)
        return "Mi dispiace, c’è stato un problema temporaneo. Riprova tra poco."

# ──────────────────────────────────────────────────────────────────────────────
# Intents & Flussi
# ──────────────────────────────────────────────────────────────────────────────
INTENT_TRANSFER = ["transfer","trasfer","taxi","trasporto","arrivare","aeroporto","stazione"]
INTENT_PARCHEGGIO = ["parcheggio","parcheggiare"]
INTENT_VIDEO = ["video","check-in","check in","istruzioni","come entrare","self check"]
INTENT_CORRENTE = ["corrente","luce","blackout","saltata la corrente","ripristino"]

def detect_intent(text: str) -> Optional[str]:
    t = text.lower()
    if any(w in t for w in INTENT_TRANSFER):   return "transfer"
    if any(w in t for w in INTENT_PARCHEGGIO): return "parcheggio"
    if any(w in t for w in INTENT_CORRENTE):   return "video"  # corrente è ramo speciale del video
    if any(w in t for w in INTENT_VIDEO):      return "video"
    return None

def start_prompt():
    return "Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* oppure *Video/Info accesso*."

# ── Transfer ──────────────────────────────────────────────────────────────────
def compute_transfer_price(start: str, dest: str) -> str:
    s = (start or "").lower()
    d = (dest or "").lower()
    airport_words = ["aeroporto","airport","galilei","pisa psa","psa"]
    if any(w in s for w in airport_words) or any(w in d for w in airport_words):
        return "50€"
    return "40€"

def transfer_step(body: str, sess: Dict[str, Any]) -> str:
    # state machine semplice
    data = sess.setdefault("transfer", {"persone": None, "orario": None, "partenza": None, "destinazione": None})
    t = body.strip()

    # estrazioni veloci
    m_num = re.search(r"\b([0-9]{1,2})(?:\s*persone| pax|pers| ospiti)?\b", t.lower())
    if m_num and not data["persone"]:
        data["persone"] = m_num.group(1)

    m_time = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m_time and not data["orario"]:
        hh, mm = m_time.group(1), m_time.group(2)
        data["orario"] = f"{int(hh):02d}:{mm}"

    # heuristics start/dest
    if any(k in t.lower() for k in ["aeroporto","airport","psa","galilei"]) and not data["partenza"]:
        data["partenza"] = "Aeroporto"
    if any(k in t.lower() for k in ["casa monic","casa di monic","monic"]) and not data["destinazione"]:
        data["destinazione"] = "Casa Monic"
    if any(k in t.lower() for k in ["belle vue","rosmini"]) and not data["destinazione"]:
        data["destinazione"] = "Belle Vue"
    if any(k in t.lower() for k in ["relais","ussero"]) and not data["destinazione"]:
        data["destinazione"] = "Relais dell’Ussero"
    if any(k in t.lower() for k in ["gina","casa di gina"]) and not data["destinazione"]:
        data["destinazione"] = "Casa di Gina"
    if any(k in t.lower() for k in ["villino"]) and not data["destinazione"]:
        data["destinazione"] = "Villino di Monic"

    # domande mancanti
    if not data["persone"]:
        return "Per il transfer, quante persone siete?"
    if not data["orario"]:
        return "A che ora desideri la partenza?"
    if not data["partenza"]:
        return "Qual è il luogo di partenza?"
    if not data["destinazione"]:
        return "Qual è la destinazione?"

    prezzo = compute_transfer_price(data["partenza"], data["destinazione"])
    return (f"Perfetto! Riepilogo:\n"
            f"• Persone: {data['persone']}\n"
            f"• Orario: {data['orario']}\n"
            f"• Partenza: {data['partenza']}\n"
            f"• Destinazione: {data['destinazione']}\n\n"
            f"Tariffa: {prezzo}.\n"
            f"Confermi che la tariffa va bene? (sì/no)\n"
            f"Poi Niccolò ti contatterà a breve per confermare.")

# ── Parcheggio (minimo, già presente) ────────────────────────────────────────
def parcheggio_step(body: str, sess: Dict[str, Any]) -> str:
    t = body.lower()
    mapping = {
        "relais": "Parcheggio pubblico Piazza Carrara (1,50€/h) a pochi metri dal Relais.",
        "ussero": "Parcheggio pubblico Piazza Carrara (1,50€/h) a pochi metri dal Relais.",
        "casa monic": "Piazza Carrara o Piazza Santa Caterina, circa 400m (1,50€/h).",
        "casa di monic": "Piazza Carrara o Piazza Santa Caterina, circa 400m (1,50€/h).",
        "monic": "Piazza Carrara o Piazza Santa Caterina, circa 400m (1,50€/h).",
        "belle vue": "Sotto al palazzo in Via Antonio Rosmini o Via Pasquale Galluppi (08–14 a pagamento, poi gratis). Custodito H24 in Via Piave (a pagamento).",
        "rosmini": "Sotto al palazzo in Via Antonio Rosmini o Via Pasquale Galluppi (08–14 a pagamento, poi gratis). Custodito H24 in Via Piave (a pagamento).",
        "gina": "Via Crispi, Piazza Aurelio Saffi o Lungarno Sidney Sonnino (1,50€/h).",
        "casa di gina": "Via Crispi, Piazza Aurelio Saffi o Lungarno Sidney Sonnino (1,50€/h).",
        "villino": "Posteggio privato incluso presso il Villino di Monic.",
        "villino di monic": "Posteggio privato incluso presso il Villino di Monic.",
    }
    for k, v in mapping.items():
        if k in t:
            return v + "\nHai bisogno di altro?"
    return "Per aiutarti col parcheggio, dimmi in quale struttura ti trovi (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)."

# ── Video / Corrente ─────────────────────────────────────────────────────────
def video_step(body: str, sess: Dict[str, Any]) -> str:
    txt = body.lower().strip()
    sess["last_topic"] = "video"
    is_corrente = sess.get("last_intent_corrente", False) or any(k in txt for k in INTENT_CORRENTE)

    # Se ha detto "corrente" e ora invia la struttura
    for name in ("relais","ussero","casa monic","casa di monic","monic","belle vue","gina","casa di gina","villino","villino di monic"):
        if name in txt and is_corrente:
            if "monic" in name and "villino" not in name:
                instr = kb_get("emergenze.casa_monic_corrente", "")
                if not instr:
                    instr = ("Per il ripristino della corrente a Casa Monic: apri la porta in cucina e verifica il quadro elettrico. "
                             "Se non basta, controlla il quadro generale accanto al portone verde (armadio cassette della posta) e riporta su la leva del contatore con la scritta COSCI.")
                vid = FALLBACK_VIDEOS.get("Casa Monic (ripristino corrente)")
                sess["last_intent_corrente"] = False
                return f"{instr}\nVideo: {vid}\nServe altro? Posso aiutarti anche con *Parcheggio* o *Transfer*."
            sess["last_intent_corrente"] = False
            return "Per questa struttura non ho una procedura corrente dedicata. Ti serve il video di self check‑in?"

    # Self check‑in video standard
    aliases = {
        "relais":"Relais dell’Ussero","ussero":"Relais dell’Ussero",
        "casa monic":"Casa Monic","casa di monic":"Casa Monic","monic":"Casa Monic",
        "belle vue":"Belle Vue","rosmini":"Belle Vue",
        "gina":"Casa di Gina","casa di gina":"Casa di Gina",
        "villino":"Villino di Monic","villino di monic":"Villino di Monic"
    }
    for k, pretty in aliases.items():
        if k in txt:
            url = kb_video_for(pretty)
            sess["last_intent_corrente"] = False
            if url:
                return f"Ecco il video di self check‑in per *{pretty}*: {url}\nHai bisogno di altro?"
            return "Per questa struttura non ho un video associato. Ti serve altro?"

    # Non capito: chiedi struttura
    if is_corrente:
        return ("Per la *corrente*, indicami la struttura (es. Casa Monic) così ti mando la procedura corretta.\n"
                "Oppure scrivi *corrente Casa Monic*.")
    return ("Hai bisogno del *video di self check‑in*? Dimmi la struttura:\n"
            "- Relais dell’Ussero\n- Casa Monic\n- Belle Vue\n- Villino di Monic\n- Casa di Gina")

# ──────────────────────────────────────────────────────────────────────────────
# Web
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "OK"

@app.route("/test", methods=["GET"])
def test_page():
    return render_template("test.html")

@app.route("/test_api", methods=["POST"])
def test_api():
    # Simula Twilio
    body = (request.form.get("Body") or "").strip()
    sender_raw = (request.form.get("From") or "").strip()
    if not sender_raw:
        # opzionale: consenti passaggio numero a mano
        sender_raw = request.form.get("Phone") or ""
    sender = normalize_sender(sender_raw)
    return _handle_message(sender, body)

@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    return _handle_message(sender, body)

def _handle_message(sender: str, body: str):
    logging.debug("Inbound da %s: %s", sender, body)
    sess = get_session(sender)

    # First-touch: lookup CiaoBooking per il numero (cache in sessione)
    if not sess.get("ciaobooking_checked"):
        try:
            first_touch_ciaobooking_lookup(sender, sess)
        except Exception as e:
            logging.error("Errore lookup CiaoBooking: %s", e)
        finally:
            sess["ciaobooking_checked"] = True

    # Se il messaggio contiene “prenotazione …<numero>”
    if re.search(r"\b(prenotazione|reservation|res)\b", body.lower()):
        rid = re.findall(r"\b(\d{6,})\b", body)
        if rid:
            res = get_reservation_by_id(rid[0])
            if res:
                sess["reservation"] = res
                return Response(twiml_message("Ho trovato la tua prenotazione, grazie. Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*?"), mimetype="application/xml")

    # Se l’utente invia SOLO un numero lungo → provalo come reservation id
    if re.fullmatch(r"\d{6,}", body):
        res = get_reservation_by_id(body)
        if res:
            sess["reservation"] = res
            return Response(twiml_message("Ho trovato la tua prenotazione, grazie. Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*?"), mimetype="application/xml")

    # Intent routing
    intent = detect_intent(body)
    if intent == "transfer":
        sess["flow"] = "transfer"
        sess["last_intent_corrente"] = False
        msg = transfer_step(body, sess)
        return Response(twiml_message(msg), mimetype="application/xml")
    if intent == "parcheggio":
        sess["flow"] = "parcheggio"
        sess["last_intent_corrente"] = False
        return Response(twiml_message(parcheggio_step(body, sess)), mimetype="application/xml")
    if intent == "video":
        sess["flow"] = "video"
        sess["last_intent_corrente"] = any(k in body.lower() for k in INTENT_CORRENTE) or sess.get("last_intent_corrente", False)
        return Response(twiml_message(video_step(body, sess)), mimetype="application/xml")

    # Se siamo già in un flow, prova a far avanzare
    flow = sess.get("flow")
    if flow == "transfer":
        return Response(twiml_message(transfer_step(body, sess)), mimetype="application/xml")
    if flow == "parcheggio":
        return Response(twiml_message(parcheggio_step(body, sess)), mimetype="application/xml")
    if flow == "video":
        return Response(twiml_message(video_step(body, sess)), mimetype="application/xml")

    # Altrimenti prompt iniziale
    return Response(twiml_message(start_prompt()), mimetype="application/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
