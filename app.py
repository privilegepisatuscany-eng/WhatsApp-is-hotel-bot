import os
import re
import json
import logging
from datetime import datetime, date
from typing import Any, Dict, Optional

from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# === Logging solido ===
LOG_LEVEL = (os.environ.get("LOG_LEVEL") or "INFO").upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    LOG_LEVEL = "INFO"
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

# === Flask ===
app = Flask(__name__)

# === OpenAI client (no proxies) ===
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY non impostata: il bot risponderà con fallback statici.")
client = OpenAI(api_key=OPENAI_API_KEY)

# === Utils locali ===
def normalize_sender(s: str) -> str:
    s = (s or "").replace("whatsapp:", "").replace("+", "").strip()
    return re.sub(r"\D", "", s)

def clamp_history(history, max_messages=12):
    return history[-max_messages:]

# === Knowledge Base ===
DEFAULT_KB = {
    "videos": {
        "relais dell’ussero": "https://youtube.com/shorts/XnBcl2T-ewM?feature=share",
        "casa monic": "https://youtube.com/shorts/YHX-7uT3itQ?feature=share",
        "belle vue": "https://youtube.com/shorts/1iqknGhIFEc?feature=share",
        "casa di gina": "https://youtube.com/shorts/Wi-mevoKB3w?feature=share",
        "villino di monic": ""
    },
    "corrente": {
        "casa monic": "https://youtube.com/shorts/UIozKt4ZrCk?feature=share"
    },
    "transfer_tariffe": {
        "aeroporto_citta": 50,
        "citta_citta": 40
    }
}
try:
    with open("knowledge_base.json", "r", encoding="utf-8") as f:
        KB = json.load(f)
        logger.info("Knowledge base caricata da knowledge_base.json")
except FileNotFoundError:
    KB = DEFAULT_KB
    logger.warning("knowledge_base.json non trovato: uso KB di default.")

# === CiaoBooking client ===
from ciao_booking_client import CiaoBookingClient

CB = CiaoBookingClient(
    base_url="https://api.ciaobooking.com",
    email=os.environ.get("CIAOBOOKING_EMAIL", ""),
    password=os.environ.get("CIAOBOOKING_PASSWORD", ""),
    locale=os.environ.get("CIAOBOOKING_LOCALE", "it"),
)

# === Memoria in RAM ===
session_store: Dict[str, Dict[str, Any]] = {}

# === Prompt di sistema ===
SYSTEM_PROMPT = (
    "Sei l’assistente di strutture ricettive a Pisa. "
    "Rispondi in modo chiaro, cortese e conciso. "
    "Usa SOLO informazioni dalla KB o dal contesto prenotazione. "
    "Se l’utente chiede Transfer, raccogli persone, orario, partenza e destinazione; applica tariffe KB. "
    "Se chiede Parcheggio o Accesso/Video, chiedi la struttura solo se non nota dal contesto. "
    "Non inventare dettagli: se mancano, chiedi in modo mirato."
)

# === Helper date ===
def _parse_ymd(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            if "T" in d or " " in d:
                return datetime.strptime(d[:19], fmt).date()
            return datetime.strptime(d, fmt).date()
        except Exception:
            continue
    return None

# === Politica video/accesso ===
def should_offer_checkin_assets_auto(booking_ctx: Dict[str, Any]) -> bool:
    res = (booking_ctx or {}).get("reservation") or {}
    if not res:
        return False
    if res.get("status") != "CONFIRMED":
        return False
    chk = (res.get("is_checkin_completed") or "").upper()
    if chk not in ("COMPLETED", "VERIFIED"):
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

def explicit_access_request_blocked(booking_ctx: Dict[str, Any]) -> bool:
    res = (booking_ctx or {}).get("reservation") or {}
    chk = (res.get("is_checkin_completed") or "").upper()
    return chk in ("", "TO_DO", "0")

# === Estrazione proprietà ===
def property_name_from_ctx(booking_ctx: Dict[str, Any]) -> Optional[str]:
    res = (booking_ctx or {}).get("reservation") or {}
    prop = (res.get("property") or {}).get("name") or res.get("property_name")
    return prop.strip() if prop else None

def normalize_property_key(name: str) -> str:
    return (name or "").strip().lower()

def kb_video_for_property(name: str) -> Optional[str]:
    key = normalize_property_key(name)
    videos = KB.get("videos", {})
    return videos.get(key) or None

def kb_power_video_for_property(name: str) -> Optional[str]:
    key = normalize_property_key(name)
    corr = KB.get("corrente", {})
    return corr.get(key)

# === Intent light ===
VIDEO_KEYWORDS = ("video", "accesso", "codice", "check-in", "checkin", "entrare", "self check")
POWER_KEYWORDS = ("corrente", "luce", "elettric")
TRANSFER_KEYWORDS = ("transfer", "taxi", "trasfer", "trasporto")
PARKING_KEYWORDS = ("parcheggio", "park", "auto", "sosta")

def wants_video(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in VIDEO_KEYWORDS)

def wants_power(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in POWER_KEYWORDS)

def wants_transfer(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in TRANSFER_KEYWORDS)

def wants_parking(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in PARKING_KEYWORDS)

# === OpenAI wrapper ===
def call_llm(messages, temperature=0.3) -> str:
    if not OPENAI_API_KEY:
        return "Ciao! Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Video/Accesso*."
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=temperature,
            messages=messages
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("Errore OpenAI: %s", e)
        return "Al momento non riesco a rispondere. Riprova tra poco, per favore."

# === CiaoBooking: bootstrap contesto ===
RES_ID_RE = re.compile(r"\b(\d{7,12})\b")

def ensure_booking_context(phone: str, text: str) -> Optional[Dict[str, Any]]:
    session = session_store.setdefault(phone, {"history": [], "booking_ctx": None, "created_at": datetime.utcnow().isoformat()})
    if session.get("booking_ctx"):
        return session["booking_ctx"]

    ctx = {}
    try:
        ctx = CB.get_booking_context(phone=phone) or {}
    except Exception as e:
        logger.error("Errore lookup CiaoBooking (phone): %s", e)

    if not ctx.get("reservation"):
        m = RES_ID_RE.search(text or "")
        if m:
            rid = m.group(1)
            try:
                ctx = CB.get_booking_context(reservation_id=rid) or ctx
            except Exception as e:
                logger.error("Errore lookup CiaoBooking (reservation_id): %s", e)

    session["booking_ctx"] = ctx or {}
    return session["booking_ctx"]

# === Business logic ===
def build_answer(phone: str, text: str) -> str:
    session = session_store.setdefault(phone, {"history": [], "booking_ctx": None, "created_at": datetime.utcnow().isoformat()})
    booking_ctx = ensure_booking_context(phone, text) or {}

    if wants_video(text) or wants_power(text):
        prop = property_name_from_ctx(booking_ctx)
        if not prop:
            return ("Per aiutarti, mi dici in quale struttura ti trovi? "
                    "(Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)")

        if explicit_access_request_blocked(booking_ctx):
            return (
                "Per motivi di sicurezza posso inviarti i link solo dopo la verifica dei documenti. "
                "Hai già inviato tutto? Se no, per favore mandaci:\n"
                "• Indirizzo di residenza\n"
                "• Foto dei documenti di identità (di tutti gli ospiti; in alternativa solo capogruppo + dati degli altri: nome, cognome, data di nascita, sesso, nazionalità)\n"
                "Appena completato, potrò inviarti il video e le istruzioni di accesso."
            )

        if wants_power(text):
            link = kb_power_video_for_property(prop)
            if link:
                return f"Ripristino corrente — {prop}: {link}"
            return "Per il ripristino corrente: verifica il quadro interno; se non torna, controlla il generale all’ingresso (cassetta contatori)."

        link = kb_video_for_property(prop)
        if link:
            return f"Video check‑in — {prop}: {link}"
        return "Sto cercando il video giusto per la tua struttura. Puoi confermarmi esattamente il nome dell’alloggio?"

    if should_offer_checkin_assets_auto(booking_ctx):
        prop = property_name_from_ctx(booking_ctx)
        if prop:
            vlink = kb_video_for_property(prop)
            msg = "Benvenuto! "
            if vlink:
                msg += f"Ecco il video di check‑in per {prop}: {vlink}\n"
            plink = kb_power_video_for_property(prop)
            if plink:
                msg += f"Se servisse il ripristino corrente: {plink}"
            return msg.strip()

    if wants_transfer(text):
        people = None
        m = re.search(r"\bsiamo in (\d{1,2})\b", text.lower())
        if m:
            people = m.group(1)
        if not people:
            m = re.search(r"\b(\d{1,2}) (persone|adulti|ospiti)\b", text.lower())
            if m:
                people = m.group(1)

        time_match = re.search(r"\b(\d{1,2})[:\.](\d{2})\b", text)
        when = f"{time_match.group(1)}:{time_match.group(2)}" if time_match else None

        t = text.lower()
        src = "aeroporto" if "aeroporto" in t else None
        dst = None
        prop = property_name_from_ctx(booking_ctx)
        if "casa monic" in t or (prop and normalize_property_key(prop) == "casa monic"):
            dst = "Casa Monic"
        elif "belle vue" in t or (prop and normalize_property_key(prop) == "belle vue"):
            dst = "Belle Vue"
        elif "relais" in t or (prop and "relais" in normalize_property_key(prop)):
            dst = "Relais dell’Ussero"
        elif "gina" in t or (prop and "gina" in normalize_property_key(prop)):
            dst = "Casa di Gina"
        elif "villino" in t or (prop and "villino" in normalize_property_key(prop)):
            dst = "Villino di Monic"

        tariffa = None
        if (src and dst) and ("aeroporto" in (src or "").lower()):
            tariffa = KB["transfer_tariffe"]["aeroporto_citta"]
        elif src or dst:
            tariffa = KB["transfer_tariffe"]["citta_citta"]

        parts = ["Perfetto, ho raccolto questi dati:"]
        parts.append(f"• Persone: {people or '—'}")
        parts.append(f"• Orario: {when or '—'}")
        parts.append(f"• Partenza: {src or '—'}")
        parts.append(f"• Destinazione: {dst or '—'}")
        if tariffa:
            parts.append(f"\nTariffa: {tariffa}€.")
        parts.append("Confermi che la tariffa va bene? (sì/no)\nIn caso affermativo, Niccolò ti contatterà a breve per la conferma.")
        return "\n".join(parts)

    if wants_parking(text):
        prop = property_name_from_ctx(booking_ctx)
        if not prop:
            return "In quale struttura soggiorni? (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)"
        return f"Parcheggio — {prop}: dimmi se hai esigenze particolari (es. orari/ingresso) e ti indico la soluzione migliore."

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"KB transfer tariffs: {json.dumps(KB.get('transfer_tariffe', {}), ensure_ascii=False)}"},
        {"role": "system", "content": "Se non è chiaro il tema, proponi: *Taxi/Transfer*, *Parcheggio*, *Video/Accesso*.\nSe la struttura è ignota e serve per rispondere, chiedila."},
        {"role": "user", "content": text},
    ]
    if booking_ctx and booking_ctx.get("reservation"):
        res = booking_ctx["reservation"]
        human_res = {
            "status": res.get("status"),
            "guest_status": res.get("guest_status"),
            "is_checkin_completed": res.get("is_checkin_completed"),
            "start_date": res.get("start_date"),
            "end_date": res.get("end_date"),
            "property_name": property_name_from_ctx(booking_ctx),
            "guests": res.get("guests"),
        }
        messages.insert(1, {"role": "system", "content": f"Reservation context: {json.dumps(human_res, ensure_ascii=False)}"})

    return call_llm(messages)

# === Router comune per messaggi ===
def handle_incoming_message(phone: str, text: str) -> str:
    # bootstrap session & context
    session_store.setdefault(phone, {"history": [], "booking_ctx": None, "created_at": datetime.utcnow().isoformat()})
    ensure_booking_context(phone, text)
    answer = build_answer(phone, text)
    # aggiorna history
    sess = session_store[phone]
    sess["history"] = clamp_history(sess["history"] + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ])
    return answer

# === Twilio Webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logger.debug("Inbound da %s: %s", sender, body)

    if body.lower() == "/reset":
        session_store.pop(sender, None)
        tw = MessagingResponse()
        tw.message("✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Video/Accesso)")
        return str(tw)

    reply = handle_incoming_message(sender, body)
    tw = MessagingResponse()
    tw.message(reply)
    return str(tw)

# === Pagina test (senza Twilio) ===
TEST_HTML = """
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Test Bot</title>
<style>
body { font-family: system-ui, -apple-system, sans-serif; background:#f6f7f9; margin:0; padding:20px;}
.card { max-width: 760px; margin:0 auto; background:#fff; border-radius:14px; box-shadow:0 6px 24px rgba(0,0,0,.08); padding:16px; }
h1 { font-size:18px; margin:0 0 12px; }
.row { display:flex; gap:8px; margin-bottom:10px; }
input, button, textarea { font-size:14px; padding:10px; border:1px solid #ddd; border-radius:10px; }
input { flex:1; }
button { cursor:pointer; }
#chat { height:420px; overflow:auto; background:#fafbfc; border:1px solid #eee; border-radius:12px; padding:12px; }
.msg { margin:8px 0; max-width:85%; padding:10px 12px; border-radius:12px; line-height:1.35; }
.me { background:#e7f1ff; margin-left:auto; }
.bot { background:#f1f3f5; }
.sys { color:#666; font-style:italic; font-size:13px; }
.small { font-size:12px; color:#666; }
</style>
</head>
<body>
<div class="card">
  <h1>Test WhatsApp Bot (no Twilio)</h1>
  <div class="row">
    <input id="phone" placeholder="Telefono (es. 3934704....)" />
    <button onclick="setPhone()">Imposta</button>
  </div>
  <div id="info" class="small">Imposta il telefono e invia il primo messaggio.</div>
  <div id="chat"></div>
  <div class="row">
    <input id="msg" placeholder="Scrivi un messaggio..." onkeydown="if(event.key==='Enter'){send()}" />
    <button onclick="send()">Invia</button>
  </div>
</div>
<script>
let PHONE = "";
const chat = document.getElementById('chat');
const info = document.getElementById('info');
function add(role, text){
  const div = document.createElement('div');
  div.className = 'msg ' + (role==='me'?'me':'bot');
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}
function setPhone(){
  const p = document.getElementById('phone').value.trim();
  if(!p){ alert('Inserisci un telefono'); return; }
  PHONE = p.replace(/\\D/g,'');
  info.textContent = 'Telefono impostato: ' + PHONE;
  const sys = document.createElement('div');
  sys.className = 'sys';
  sys.textContent = 'Conversazione pronta. Inserisci messaggi.';
  chat.appendChild(sys);
}
async function send(){
  if(!PHONE){ alert('Imposta prima il telefono'); return; }
  const t = document.getElementById('msg').value.trim();
  if(!t) return;
  document.getElementById('msg').value = '';
  add('me', t);
  try{
    const r = await fetch('/test_api', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ phone: PHONE, text: t })
    });
    const j = await r.json();
    add('bot', (j.reply || j.error || '(nessuna risposta)'));
  }catch(e){
    add('bot', 'Errore: ' + e);
  }
}
</script>
</body>
</html>
"""

@app.route("/test", methods=["GET"])
def test_page():
    return Response(TEST_HTML, mimetype="text/html")

# --- TEST API: invio messaggi senza Twilio ---
@app.post("/test_api")
def test_api():
    payload = request.get_json(silent=True) or {}
    phone = (payload.get("phone") or request.form.get("phone") or "").strip()
    text  = (
        payload.get("text")           # <--- FIX: supporta 'text'
        or payload.get("message")
        or payload.get("body")
        or request.form.get("text")
        or request.form.get("message")
        or request.form.get("body")
        or ""
    ).strip()

    if not phone or not text:
        logger.error("Bad /test_api payload. CT=%s, json=%s, form=%s",
                     request.headers.get("Content-Type"), payload, dict(request.form))
        return jsonify({"ok": False, "error": "missing phone or message"}), 400

    try:
        reply_text = handle_incoming_message(phone, text)
    except Exception as e:
        logger.exception("Errore handle_incoming_message: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "reply": reply_text}), 200

# === Healthcheck e root ===
@app.route("/", methods=["GET"])
def root():
    return Response("OK", mimetype="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=(LOG_LEVEL == "DEBUG"))
