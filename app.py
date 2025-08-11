import os
import re
import json
import time
import logging
from datetime import datetime
from typing import Dict, Any, List

import requests
from flask import Flask, request, jsonify, Response

try:
    # Se c'è il client, lo usiamo; altrimenti proseguiamo senza hard fail
    from ciao_booking_client import CiaoBookingClient
except Exception:  # pragma: no cover
    CiaoBookingClient = None

try:
    # Se hai un utils.py va bene; altrimenti usiamo i fallback più sotto
    from utils import normalize_sender as normalize_sender_ext  # type: ignore
    from utils import clamp_history as clamp_history_ext  # type: ignore
except Exception:
    normalize_sender_ext = None
    clamp_history_ext = None


# ----------------- Logging -----------------
_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _level = "INFO"
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ----------------- Flask -----------------
app = Flask(__name__)


# ----------------- Helpers (fallback) -----------------
def normalize_sender(s: str) -> str:
    """Normalizza un identificativo mittente in solo cifre (per WhatsApp è un telefono)."""
    if normalize_sender_ext:
        try:
            return normalize_sender_ext(s)
        except Exception:
            pass
    return re.sub(r"\D+", "", s or "")


def clamp_history(hist: List[Dict[str, str]], max_items: int = 12) -> List[Dict[str, str]]:
    """Taglia la history per non farla esplodere."""
    if clamp_history_ext:
        try:
            return clamp_history_ext(hist, max_items=max_items)
        except Exception:
            pass
    return hist[-max_items:] if len(hist) > max_items else hist


def extract_intent(text: str) -> str:
    """Grezzissimo router di intent per ridurre il numero di regole hardcoded."""
    t = (text or "").lower()
    if any(w in t for w in ["transfer", "trasfer", "trasnfer", "taxi", "ncc"]):
        return "transfer"
    if any(w in t for w in ["parcheggio", "parking", "parcheggiare"]):
        return "parking"
    if any(w in t for w in ["video", "accesso", "check in", "check-in", "codice", "self check"]):
        return "access"
    if any(w in t for w in ["corrente", "luce", "salta la corrente", "blackout"]):
        return "power"
    if any(w in t for w in ["check out", "checkout", "check-out"]):
        return "checkout"
    if any(w in t for w in ["colazione", "breakfast"]):
        return "breakfast"
    return "generic"


# ----------------- OpenAI (senza SDK) -----------------
def call_openai_chat(messages: List[Dict[str, str]],
                     model: str = None,
                     temperature: float = 0.3,
                     timeout: int = 30) -> str:
    """Chiama l'endpoint Chat Completions con requests, evitando l’SDK (fix al problema proxies)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY mancante")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": messages,
        "temperature": temperature,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


# ----------------- Knowledge Base -----------------
def load_kb() -> Dict[str, Any]:
    path = os.environ.get("KB_PATH", "knowledge_base.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            kb = json.load(f)
            logger.info("KB caricata da %s", path)
            return kb
    except FileNotFoundError:
        logger.warning("KB non trovata (%s). Procedo con KB vuota.", path)
        return {}
    except Exception as e:
        logger.error("Errore caricando KB: %s", e)
        return {}


KB: Dict[str, Any] = load_kb()

# Prepara un “riassunto” KB per il system prompt (limite semplice)
def kb_to_prompt(kb: Dict[str, Any], max_len: int = 10000) -> str:
    try:
        txt = json.dumps(kb, ensure_ascii=False)
        if len(txt) > max_len:
            txt = txt[:max_len] + " …"
        return txt
    except Exception:
        return ""


KB_SNIPPET = kb_to_prompt(KB)


# ----------------- CiaoBooking -----------------
CB = None
if CiaoBookingClient:
    try:
        CB = CiaoBookingClient(
            base_url=os.environ.get("CIAOBOOKING_BASE_URL", "https://api.ciaobooking.com"),
            email=os.environ.get("CIAOBOOKING_EMAIL", ""),
            password=os.environ.get("CIAOBOOKING_PASSWORD", ""),
            locale=os.environ.get("CIAOBOOKING_LOCALE", "it"),
        )
        logger.info("CiaoBooking client inizializzato.")
    except Exception as e:
        logger.error("CiaoBooking init fallita: %s", e)


def cb_lookup_once(session: Dict[str, Any], phone: str, user_text: str) -> None:
    """Al primo messaggio per un numero, prova a risolvere il cliente e la prenotazione."""
    if session.get("booking_checked"):
        return
    session["booking_checked"] = True
    session.setdefault("booking_ctx", {})

    if not CB:
        logger.info("CiaoBooking non disponibile (client mancante). Skip lookup.")
        return

    try:
        client = CB.find_client_by_phone(phone)
        if client:
            session["booking_ctx"]["client"] = client
            logger.info("CiaoBooking: client risolto per %s", phone)
        else:
            logger.info("CiaoBooking: client non trovato (%s)", phone)
    except Exception as e:
        logger.error("Errore lookup client CiaoBooking: %s", e)

    # Se l’utente ha scritto un numero di prenotazione, proviamo a prenderla
    m = re.search(r"\b(\d{6,})\b", user_text or "")
    if m:
        res_id = m.group(1)
        try:
            res = CB.get_reservation_by_id(res_id)
            if res:
                session["booking_ctx"]["reservation"] = res
                logger.info("CiaoBooking: reservation %s risolta", res_id)
        except Exception as e:
            logger.error("CiaoBooking reservation error: %s", e)


# ----------------- Stato conversazioni -----------------
# session_store[phone] = {
#   "history": [{"role": "user"/"assistant"/"system", "content": "..."}],
#   "booking_checked": bool,
#   "booking_ctx": {...},
#   "topic": "transfer|parking|access|power|generic",
#   "transfer_draft": {"people":..., "time":..., "from":..., "to":...},
# }
session_store: Dict[str, Dict[str, Any]] = {}


SYSTEM_INSTRUCTIONS = (
    "Sei un assistente per strutture ricettive a Pisa. "
    "Parla in modo chiaro, cortese e conciso. "
    "Usa SOLO la KB e/o i dati prenotazione disponibili; non inventare. "
    "Tariffe transfer: Aeroporto ↔ Città 50€, Città ↔ Città 40€. "
    "Se l’utente chiede transfer, raccogli dati: persone, orario, partenza, destinazione. "
    "Per parcheggio, chiedi o deduci la struttura e rispondi con info specifiche dalla KB. "
    "Per corrente/blackout alla 'Casa Monic' ricorda: quadro in cucina; se non torna, quadro generale al portone verde (cassetta posta, contatore COSCI). "
    "Se ci sono video in KB relativi alla struttura richiesta, condividi il link. "
    "Evita di ripartire da capo dopo una conferma: prosegui nel flusso in corso."
)


def build_messages(session: Dict[str, Any], user_text: str) -> List[Dict[str, str]]:
    """Costruisce il contesto conversazionale per il modello."""
    history = session.get("history", [])
    booking_ctx = session.get("booking_ctx", {})
    ctx_str = json.dumps(booking_ctx, ensure_ascii=False) if booking_ctx else "{}"

    system = (
        SYSTEM_INSTRUCTIONS +
        "\n\n[KB]\n" + (KB_SNIPPET or "") +
        "\n\n[Prenotazione]\n" + ctx_str
    )

    msgs = [{"role": "system", "content": system}]
    msgs.extend(history[-8:])  # un po’ di contesto recente
    msgs.append({"role": "user", "content": user_text})
    return msgs


def postprocess_reply(text: str, session: Dict[str, Any], user_text: str) -> str:
    """Piccoli fix: aggiungi video se l’utente ha chiesto e la KB ce l’ha."""
    t = (user_text or "").lower()

    # Se domanda video + struttura
    if any(w in t for w in ["video", "accesso", "check in", "check-in", "self check"]):
        # Trova struttura citata
        structures = ["Relais dell’Ussero", "Casa Monic", "Belle Vue", "Villino di Monic", "Casa di Gina",
                      "relais", "ussero", "monic", "belle vue", "gina", "villino"]
        found = None
        for s in structures:
            if s.lower() in t:
                found = s
                break

        # Cerca in KB eventuali video
        videos = KB.get("videos") or KB.get("video") or {}
        if found and videos:
            # normalizza chiavi
            for k, payload in videos.items():
                if k.lower() in found.lower() or found.lower() in k.lower():
                    # scegli `checkin` o link generico
                    link = payload.get("checkin") or payload.get("selfcheckin") or payload.get("url") or ""
                    if link:
                        text += f"\n\n🎥 Video accesso ({k}): {link}"
                    # power/reset
                    power = payload.get("power") or payload.get("corrente")
                    if power and any(w in t for w in ["corrente", "luce", "blackout"]):
                        text += f"\n🔌 Ripristino corrente ({k}): {power}"
                    break

    # Se parla di corrente + Casa Monic, assicurati che la sequenza base ci sia
    if "casa monic" in t and any(w in t for w in ["corrente", "luce", "blackout"]):
        if "COSCI" not in text and "Cosci" not in text:
            text += (
                "\n\n➕ Promemoria: se non torna la luce, controlla il quadro generale "
                "accanto al portone verde nelle cassette della posta. Contatore con scritto *COSCI*."
            )

    return text


def respond_text(sender: str, user_text: str) -> str:
    """Flusso principale: lookup CB una sola volta, LLM con KB+ctx, stato conversazione."""
    session = session_store.setdefault(sender, {"history": [], "booking_checked": False})

    # Lookup CB solo al primo messaggio della sessione
    try:
        cb_lookup_once(session, sender, user_text)
    except Exception as e:
        logger.error("cb_lookup_once errore: %s", e)

    # Intent light (solo per non perdere il filo)
    if "topic" not in session or not session["topic"] or extract_intent(user_text) != "generic":
        session["topic"] = extract_intent(user_text)

    # Costruisci messaggi per il modello
    messages = build_messages(session, user_text)

    # Chiamata modello
    reply = call_openai_chat(messages, temperature=0.3)

    # Postprocess (aggiunta link video se rilevati)
    reply = postprocess_reply(reply, session, user_text)

    # Aggiorna history
    session["history"] = clamp_history(session.get("history", []) + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ])

    return reply


def make_twiliml(text: str) -> str:
    """TwiML minimale senza dipendenze se vuoi evitare twilio SDK; ma noi abbiamo già twilio nel reqs."""
    try:
        from twilio.twiml.messaging_response import MessagingResponse
        r = MessagingResponse()
        r.message(text)
        return str(r)
    except Exception:
        # Fallback ultra minimale
        return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{text}</Message></Response>'


# ----------------- Routes -----------------
@app.route("/", methods=["GET"])
def index():
    return "OK", 200


TEST_HTML = """
<!doctype html>
<html lang="it">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Chat – Privilege Pisa</title>
<style>
body { font-family: system-ui, Arial, sans-serif; margin: 0; background:#f6f6f8; }
.container { max-width: 820px; margin: 24px auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.07); overflow: hidden; }
.header { padding: 16px 20px; border-bottom: 1px solid #eee; display:flex; gap:12px; align-items:center; }
.header input { padding:10px 12px; border:1px solid #ddd; border-radius:8px; outline:none; }
.header button { padding:10px 14px; border:none; background:#111827; color:#fff; border-radius:8px; cursor:pointer; }
.chat { padding: 16px 20px; height: 60vh; overflow-y: auto; background: #fafafa; }
.msg { margin: 10px 0; display:flex; }
.msg.me { justify-content: flex-end; }
.bubble { max-width: 70%; padding: 10px 12px; border-radius: 12px; }
.me .bubble { background:#111827; color:#fff; border-bottom-right-radius: 4px; }
.bot .bubble { background:#e5e7eb; color:#111827; border-bottom-left-radius: 4px; }
.footer { padding: 16px 20px; border-top:1px solid #eee; display:flex; gap:10px; }
.footer input { flex:1; padding:12px; border:1px solid #ddd; border-radius:10px; outline:none; }
.footer button { padding:12px 16px; border:none; background:#111827; color:#fff; border-radius:10px; cursor:pointer; }
.small { font-size:12px; color:#6b7280; }
</style>
<div class="container">
  <div class="header">
    <div><strong>Test Chat</strong> <span class="small">– Inserisci numero telefono (solo cifre)</span></div>
    <input id="phone" placeholder="es. 393470416638" />
    <button onclick="setPhone()">Usa numero</button>
  </div>
  <div id="chat" class="chat"></div>
  <div class="footer">
    <input id="text" placeholder="Scrivi un messaggio..." onkeydown="if(event.key==='Enter')send()"/>
    <button onclick="send()">Invia</button>
  </div>
</div>
<script>
let phone = "";
const chat = document.getElementById('chat');

function setPhone(){
  const p = document.getElementById('phone').value.trim();
  if(!p){ alert('Inserisci un numero'); return; }
  phone = p.replace(/\\D+/g,'');
  add('bot','✅ Conversazione pronta. Inserisci telefono e invia il primo messaggio.');
}

function add(who, text){
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + who;
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  wrap.appendChild(b);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
}

async function send(){
  const t = document.getElementById('text').value.trim();
  if(!t){ return; }
  if(!phone){ alert('Prima imposta il numero'); return; }
  add('me', t);
  document.getElementById('text').value = '';
  try{
    const r = await fetch('/webhook', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-Test-Client':'1'},
      body: JSON.stringify({ phone: phone, body: t })
    });
    const data = await r.json();
    add('bot', data.reply || '(nessuna risposta)');
  }catch(e){
    add('bot', 'Errore: ' + e);
  }
}
</script>
"""

@app.route("/test", methods=["GET"])
def test_page():
    return Response(TEST_HTML, mimetype="text/html")


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    - Se arriva da Twilio: form-encoded con 'From' e 'Body' -> risponde in TwiML.
    - Se arriva dal tester: JSON {phone, body} + header X-Test-Client: 1 -> risponde JSON.
    """
    is_test = request.headers.get("X-Test-Client") == "1" or request.is_json

    if is_test:
        data = request.get_json(silent=True) or {}
        sender = normalize_sender(str(data.get("phone", "")))
        body = (data.get("body") or "").strip()
    else:
        sender_raw = request.form.get("From", "")
        body = (request.form.get("Body") or "").strip()
        sender = normalize_sender(sender_raw)

    logger.debug("Inbound da %s: %s", sender, body)

    if not sender:
        out = "Numero non valido. Inserisci un numero di telefono (solo cifre)."
        if is_test:
            return jsonify({"reply": out})
        return make_twiliml(out)

    try:
        reply = respond_text(sender, body)
    except Exception as e:
        logger.exception("Errore in respond_text: %s", e)
        reply = "Mi dispiace, c'è stato un problema temporaneo. Riprova tra poco."

    if is_test:
        return jsonify({"reply": reply})
    else:
        return make_twiliml(reply)
