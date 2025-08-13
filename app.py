# app.py
import os
import re
import json
import logging
from datetime import datetime, date
from typing import Any, Dict, Optional

from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# === Logging ===
LOG_LEVEL = (os.environ.get("LOG_LEVEL") or "INFO").upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    LOG_LEVEL = "INFO"
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

# === Flask ===
app = Flask(__name__)

# === OpenAI client ===
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY non impostata: risposte fallback.")
client = OpenAI(api_key=OPENAI_API_KEY)

# === Utils ===
def normalize_sender(s: str) -> str:
    s = s or ""
    s = s.replace("whatsapp:", "").replace("+", "").strip()
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
# session_store[phone] = {
#   "history": [...],
#   "booking_ctx": {...},
#   "created_at": ISO,
#   "pending_confirm": {...} | None,
#   "reservation_confirmed": bool
# }
session_store: Dict[str, Dict[str, Any]] = {}

# === Prompt di sistema minimal ===
SYSTEM_PROMPT = (
    "Sei l’assistente di strutture ricettive a Pisa. "
    "Rispondi in modo chiaro, cortese e conciso. "
    "Usa SOLO informazioni dalla KB o dal contesto prenotazione. "
    "Se l’utente chiede Transfer, raccogli persone, orario, partenza e destinazione; applica tariffe KB. "
    "Se chiede Parcheggio o Accesso/Video, chiedi la struttura solo se non nota dal contesto. "
    "Non inventare dettagli: se mancano, chiedi in modo mirato. "
    "NON dire mai 'non posso accedere ai dettagli della prenotazione': "
    "se il contesto non è disponibile, chiedi gentilmente il nome della struttura. "
    "Se conosci il nome dell'ospite, salutalo usando il suo nome."
)

# === Date helper ===
def _parse_ymd(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    try:
        return date.fromisoformat(d[:10])
    except Exception:
        return None

# === Politiche video/accesso ===
def should_offer_checkin_assets_auto(booking_ctx: Dict[str, Any]) -> bool:
    res = (booking_ctx or {}).get("reservation") or {}
    if not res:
        return False
    if (res.get("status") or "").upper() != "CONFIRMED":
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
    if not res:
        return False
    chk = (res.get("is_checkin_completed") or "").upper()
    return chk in ("TO_DO", "0", "")

# === Property helpers ===
ALIASES = {
    "casa monic": ["casa monic", "monic", "casa di monic", "casa di monic", "villino di monic?"],
    "relais dell’ussero": ["relais", "ussero", "relais dell'ussero", "relais dell’ussero"],
    "belle vue": ["belle vue", "bellevue"],
    "casa di gina": ["casa di gina", "gina"],
    "villino di monic": ["villino di monic", "villino monic", "villino"],
}
def normalize_property_key(name: str) -> str:
    return (name or "").strip().lower()

def extract_property_from_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    for canonical, arr in ALIASES.items():
        for a in arr:
            if a in t:
                return canonical
    for k in KB.get("videos", {}):
        if k in t:
            return k
    return None

def property_name_from_ctx(booking_ctx: Dict[str, Any]) -> Optional[str]:
    res = (booking_ctx or {}).get("reservation") or {}
    prop = (res.get("property") or {}).get("name") or res.get("property_name")
    return prop.strip() if prop else None

def kb_video_for_property(name: str) -> Optional[str]:
    key = normalize_property_key(name)
    return KB.get("videos", {}).get(key) or None

def kb_power_video_for_property(name: str) -> Optional[str]:
    key = normalize_property_key(name)
    return KB.get("corrente", {}).get(key) or None

# === Intent leggeri ===
VIDEO_KEYWORDS = ("video", "accesso", "codice", "check-in", "checkin", "entrare", "self check")
POWER_KEYWORDS = ("corrente", "luce", "elettric", "power")
TRANSFER_KEYWORDS = ("transfer", "taxi", "trasfer", "trasporto")
PARKING_KEYWORDS = ("parcheggio", "park", "auto", "sosta")
YES_WORDS = ("si", "sì", "ok", "va bene", "certo", "confermo", "yes")
NO_WORDS = ("no", "non", "negativo")

def wants_video(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in VIDEO_KEYWORDS)

def wants_power(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in POWER_KEYWORDS)

def wants_transfer(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in TRANSFER_KEYWORDS)

def wants_parking(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in PARKING_KEYWORDS)

def is_yes(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(t.startswith(w) or t == w for w in YES_WORDS)

def is_no(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(t == w for w in NO_WORDS)

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
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Errore OpenAI: %s", e)
        return "Al momento non riesco a rispondere. Riprova tra poco, per favore."

# === CiaoBooking bootstrap ===
RES_ID_RE = re.compile(r"\b(\d{7,12})\b")

def get_booking_context(phone: str, text: str) -> Dict[str, Any]:
    reservation_id = None
    m = RES_ID_RE.search(text or "")
    if m:
        reservation_id = m.group(1)
    tried = bool(reservation_id)
    found = False
    ctx = {}
    try:
        ctx = CB.get_booking_context(phone=phone or None, reservation_id=reservation_id or None) or {}
        if reservation_id:
            found = bool(ctx.get("reservation"))
    except Exception as e:
        logger.error("Errore lookup CiaoBooking: %s", e)
        ctx = {}
    ctx.setdefault("_lookup", {})["rid_tried"] = reservation_id if tried else None
    ctx["_lookup"]["rid_found"] = found
    return ctx

# === Core business reply ===
def build_answer(phone: str, text: str, booking_ctx: Dict[str, Any], session: Dict[str, Any]) -> str:
    # reset conv?
    if (text or "").strip().lower() == "/reset":
        session_store.pop(phone, None)
        return "✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Video/Accesso)"

    # Saluto personalizzato quando arriva un saluto & abbiamo il nome
    client_name = ((booking_ctx.get("client") or {}).get("name") or "").strip()
    is_greeting = any(g in (text or "").lower() for g in ["ciao", "buongiorno", "salve", "hey"])
    if is_greeting and client_name:
        # Se abbiamo anche una reservation non ancora confermata nella sessione → proponi conferma
        res = (booking_ctx.get("reservation") or {})
        prop = property_name_from_ctx(booking_ctx)
        if res and prop:
            start = (res.get("start_date") or "")[:10]
            end = (res.get("end_date") or "")[:10]
            session["pending_confirm"] = {
                "prop": prop,
                "start": start,
                "end": end,
                "reservation_id": res.get("id"),
            }
            session["reservation_confirmed"] = False
            return f"Ciao {client_name.split()[0]}! Confermi che la tua prenotazione è presso *{prop}* dal *{start}* al *{end}*?"
        # altrimenti semplice saluto
        return f"Ciao {client_name.split()[0]}! Come posso aiutarti oggi? Se hai bisogno di informazioni su transfer, parcheggio o accesso, fammi sapere!"

    # Gestisci conferma prenotazione
    if session.get("pending_confirm"):
        if is_yes(text):
            session["reservation_confirmed"] = True
            confirm = session["pending_confirm"]
            prop = confirm["prop"]
            # con conferma, se policy OK invia link automatici
            # controlla stato check-in
            res = (booking_ctx.get("reservation") or {})
            chk = (res.get("is_checkin_completed") or "").upper()
            parts = [f"Perfetto! Prenotazione confermata per *{prop}*."]
            if chk in ("COMPLETED", "VERIFIED"):
                v = kb_video_for_property(prop)
                p = kb_power_video_for_property(prop)
                if v:
                    parts.append(f"Video check‑in: {v}")
                if p:
                    parts.append(f"Ripristino corrente: {p}")
                parts.append("Se ti serve *Parcheggio* o *Transfer*, dimmelo pure.")
            else:
                parts.append(
                    "Per motivi di sicurezza posso inviare i link di accesso dopo la verifica documenti.\n"
                    "Hai già inviato:\n"
                    "• Indirizzo di residenza\n"
                    "• Foto documenti per tutti gli ospiti (oppure solo capogruppo + dati degli altri)?"
                )
            # consumiamo la pending
            session["pending_confirm"] = None
            return "\n".join(parts)
        elif is_no(text):
            session["pending_confirm"] = None
            session["reservation_confirmed"] = False
            return ("Nessun problema. Per aiutarti, mi dici in quale struttura ti trovi? "
                    "(Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)")

    # prova a determinare la property dal contesto o dal testo
    prop_from_ctx = property_name_from_ctx(booking_ctx)
    prop_from_text = extract_property_from_text(text)
    prop = prop_from_ctx or prop_from_text

    # Se l'utente manda un ID prenotazione inesistente → messaggio dedicato
    _lookup = (booking_ctx or {}).get("_lookup") or {}
    if _lookup.get("rid_tried") and not _lookup.get("rid_found"):
        return (
            f"Non trovo la prenotazione {_lookup['rid_tried']}. "
            "Per aiutarti subito, mi indichi il nome della struttura? "
            "(Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)"
        )

    # 1) Richieste esplicite: video / corrente
    if wants_video(text) or wants_power(text):
        if not prop:
            # se abbiamo pending_confirm in sessione, riproponi la conferma chiara
            if session.get("pending_confirm"):
                pc = session["pending_confirm"]
                return (f"Confermi *{pc['prop']}* dal *{pc['start']}* al *{pc['end']}*? "
                        "Se sì, posso inviarti i link utili.")
            return ("Per aiutarti, mi dici in quale struttura ti trovi? "
                    "(Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)")

        # blocco solo se abbiamo reservation e check-in TO_DO
        if explicit_access_request_blocked(booking_ctx):
            return (
                "Per motivi di sicurezza posso inviarti i link solo dopo la verifica dei documenti. "
                "Hai già inviato tutto? Se no, per favore mandaci:\n"
                "• Indirizzo di residenza\n"
                "• Foto dei documenti di identità (di tutti gli ospiti; in alternativa solo capogruppo + dati degli altri: nome, cognome, data di nascita, sesso, nazionalità)\n"
                "Appena completato, potrò inviarti il video e le istruzioni di accesso."
            )

        if wants_power(text):
            plink = kb_power_video_for_property(prop)
            if plink:
                return f"Ripristino corrente — {prop}: {plink}"
            return "Per il ripristino corrente: verifica il quadro interno; se non torna, controlla il generale all’ingresso (cassetta contatori)."

        vlink = kb_video_for_property(prop)
        if vlink:
            return f"Video check‑in — {prop}: {vlink}"
        return "Sto cercando il video giusto per la tua struttura. Puoi confermare esattamente il nome dell’alloggio?"

    # 2) Invio automatico link se policy OK (senza richiesta esplicita)
    if should_offer_checkin_assets_auto(booking_ctx):
        if prop:
            parts = ["Benvenuto!"]
            vlink = kb_video_for_property(prop)
            if vlink:
                parts.append(f"Video check‑in per {prop}: {vlink}")
            plink = kb_power_video_for_property(prop)
            if plink:
                parts.append(f"Ripristino corrente: {plink}")
            return "\n".join(parts)

    # 3) Transfer: raccogliamo dati base
    if wants_transfer(text):
        t = (text or "").lower()
        # persone
        people = None
        m = re.search(r"\bsiamo in (\d{1,2})\b", t)
        if not m:
            m = re.search(r"\b(\d{1,2}) (persone|adulti|ospiti)\b", t)
        if m:
            people = m.group(1)

        # orario
        time_match = re.search(r"\b(\d{1,2})[:\.](\d{2})\b", text or "")
        when = f"{time_match.group(1)}:{time_match.group(2)}" if time_match else None

        # partenza/destinazione (grezzo)
        src = "aeroporto" if "aeroporto" in t else None
        dst = None
        if prop:
            dst = {
                "casa monic": "Casa Monic",
                "relais dell’ussero": "Relais dell’Ussero",
                "belle vue": "Belle Vue",
                "casa di gina": "Casa di Gina",
                "villino di monic": "Villino di Monic",
            }.get(normalize_property_key(prop), prop)

        # tariffa
        tariffa = None
        if (src and dst) and ("aeroporto" in (src or "")):
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
        parts.append("Confermi? (sì/no) In caso affermativo, Niccolò ti contatterà a breve.")
        return "\n".join(parts)

    # 4) Parcheggio
    if wants_parking(text):
        if not prop:
            return "In quale struttura soggiorni? (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)"
        return f"Parcheggio — {prop}: dimmi se hai esigenze particolari (orari/ingresso) e ti indico la soluzione migliore."

    # 5) Default: LLM con contesto
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"KB transfer tariffs: {json.dumps(KB.get('transfer_tariffe', {}), ensure_ascii=False)}"},
        {"role": "system", "content": ("Se non è chiaro il tema, proponi: *Taxi/Transfer*, *Parcheggio*, *Video/Accesso*.\n"
                                       "Se la struttura è ignota e serve per rispondere, chiedila.")},
        {"role": "user", "content": text or ""},
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

def handle_incoming_message(phone: str, text: str) -> str:
    # bootstrap session + booking ctx
    session = session_store.setdefault(phone, {
        "history": [],
        "booking_ctx": None,
        "created_at": datetime.utcnow().isoformat(),
        "pending_confirm": None,
        "reservation_confirmed": False,
    })
    booking_ctx = get_booking_context(phone, text)
    session["booking_ctx"] = booking_ctx

    answer = build_answer(phone, text, booking_ctx, session)
    session["history"] = clamp_history(session["history"] + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ])
    return answer

# === Twilio webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logger.debug("Inbound da %s: %s", sender, body)

    reply = handle_incoming_message(sender, body)

    twiml = MessagingResponse()
    twiml.message(reply)
    return str(twiml)

# === Endpoint debug: vedere contesto trovato ===
@app.get("/debug/ctx")
def debug_ctx():
    phone = (request.args.get("phone") or "").strip()
    rid = (request.args.get("rid") or "").strip()
    if not phone and not rid:
        return jsonify({"error": "specify phone= or rid="}), 400
    try:
        ctx = CB.get_booking_context(phone=phone or None, reservation_id=rid or None)
        return jsonify({"ok": True, "ctx": ctx}), 200
    except Exception as e:
        logger.exception("debug_ctx error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# === Pagina test locale ===
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
input, button { font-size:14px; padding:10px; border:1px solid #ddd; border-radius:10px; }
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
      body: JSON.stringify({ phone: PHONE, message: t })
    });
    const j = await r.json();
    add('bot', j.answer || j.reply || '(nessuna risposta)');
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

# --- API test senza Twilio ---
@app.post("/test_api")
def test_api():
    payload = request.get_json(silent=True) or {}
    phone = (payload.get("phone") or request.form.get("phone") or "").strip()
    text  = (
        payload.get("message")
        or payload.get("body")
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

    return jsonify({"ok": True, "answer": reply_text}), 200

# === Healthcheck / root ===
@app.route("/", methods=["GET"])
def root():
    return Response("OK", mimetype="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=(LOG_LEVEL == "DEBUG"))
