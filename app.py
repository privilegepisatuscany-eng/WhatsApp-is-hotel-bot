import os, json, time, logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
import requests

# === Logging robusto (accetta "debug", "info", ecc.) ===
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in {"CRITICAL","ERROR","WARNING","INFO","DEBUG"}:
    LOG_LEVEL = "INFO"
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

# === OpenAI ===
import openai
openai.api_key = os.environ.get("OPENAI_API_KEY")

# === CiaoBooking client (inline leggero) ===
CIAOBOOKING_BASE = os.environ.get("CIAOBOOKING_BASE", "https://api.ciaobooking.com")
CIAOBOOKING_EMAIL = os.environ.get("CIAOBOOKING_EMAIL", "")
CIAOBOOKING_PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD", "")
CIAOBOOKING_LOCALE = os.environ.get("CIAOBOOKING_LOCALE", "it")

_session_cache = {
    # sender: {"ctx": {...}, "exp": epoch}
}

def _cb_login():
    url = f"{CIAOBOOKING_BASE}/api/public/login"
    files = {
        "email": (None, CIAOBOOKING_EMAIL),
        "password": (None, CIAOBOOKING_PASSWORD),
        "source": (None, "bot"),
    }
    r = requests.post(url, files=files, timeout=7)
    r.raise_for_status()
    data = r.json()["data"]
    token = data["token"]
    exp = data["expiresAt"]
    logging.info("CiaoBooking login OK; token valid until %s", exp)
    return token

def _cb_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def cb_find_client_by_phone(token, phone_normalized):
    # GET /api/public/clients/paginated?search=... (NB: GET!)
    params = {
        "limit": "5",
        "page": "1",
        "search": phone_normalized,
        "order": "asc",
        "sortBy[]": "name",
    }
    url = f"{CIAOBOOKING_BASE}/api/public/clients/paginated"
    r = requests.get(url, headers=_cb_headers(token), params=params, timeout=7)
    if r.status_code >= 400:
        logging.error("CiaoBooking error: %s", r.text)
        r.raise_for_status()
    data = r.json().get("data", {}).get("collection", [])
    if not data:
        logging.info("CiaoBooking: client non trovato (%s)", phone_normalized)
        return None
    return data[0]

def cb_get_reservation_by_id(token, res_id):
    url = f"{CIAOBOOKING_BASE}/api/public/reservations/{res_id}"
    r = requests.get(url, headers=_cb_headers(token), timeout=7)
    if r.status_code == 404:
        logging.error("CiaoBooking reservation error: %s", r.text)
        return None
    r.raise_for_status()
    return r.json().get("data")

def normalize_sender(v):
    # es. "whatsapp:+39347...." -> "39347..."
    v = v or ""
    v = v.replace("whatsapp:", "")
    v = v.replace("+", "")
    return v.strip()

# === KB in JSON (caricata una volta) ===
with open("knowledge_base.json", "r", encoding="utf-8") as f:
    KB = json.load(f)

# Mappa rapida video (per prompt e risposte)
VIDEOS = KB.get("videos", {})

app = Flask(__name__)

def _load_or_make_context(sender, incoming_text):
    now = int(time.time())
    entry = _session_cache.get(sender)

    # scadenza cache 2h
    if entry and entry.get("exp", 0) > now:
        return entry["ctx"]

    ctx = {
        "sender": sender,
        "has_booking": False,
        "booking": None,    # raw from API if available
        "property": None,   # es. "Casa Monic", "Belle Vue", ...
        "docs_sent": None,  # se riusciamo a dedurre in futuro
    }

    # Primo messaggio: prova lookup
    try:
        token = _cb_login()
        # 1) client via telefono
        cli = cb_find_client_by_phone(token, sender)
        if cli:
            ctx["has_booking"] = True
            # qui potresti anche cercare le reservation del client se serve:
            # (l'API "reservations" non espone direttamente "search by client" nella spec qui,
            # quindi teniamo minimal e lasciamo all'utente fornirci un eventuale id)
        else:
            # 2) se il messaggio contiene un possibile reservation id, prova
            # estrai numeri lunghi:
            import re
            m = re.search(r"\b(\d{7,})\b", incoming_text)
            if m:
                res_id = m.group(1)
                res = cb_get_reservation_by_id(token, res_id)
                if res:
                    ctx["has_booking"] = True
                    ctx["booking"] = res
                    # prova dedurre property name se presente
                    prop = res.get("property", {}).get("name") or None
                    ctx["property"] = prop
    except requests.HTTPError as e:
        logging.error("Errore lookup CiaoBooking: %s", e)
    except Exception as e:
        logging.error("Errore generico lookup CiaoBooking: %s", e)

    _session_cache[sender] = {"ctx": ctx, "exp": now + 7200}
    return ctx

def build_system_prompt(ctx):
    """
    Prompt minimale: niente flussi rigidi, l'LLM usa KB + eventuale contesto prenotazione.
    """
    kb_compact = {
        "addresses": KB.get("addresses", {}),
        "parking": KB.get("parking", {}),
        "transfer": KB.get("transfer", {}),
        "emergency": KB.get("emergency", {}),
        "rules": KB.get("rules", {}),
        "videos": KB.get("videos", {}),
        "checkin": KB.get("checkin", {}),
        "breakfast": KB.get("breakfast", {}),
        "wifi_hint": KB.get("wifi_hint", ""),
    }

    sys = (
        "Sei un assistente per una struttura ricettiva a Pisa. "
        "Rispondi in italiano, con tono cordiale e conciso. "
        "Usa esclusivamente le informazioni nella KB fornita e nel contesto prenotazione.\n\n"
        f"CONTESTO_PRENOTAZIONE: {json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"KNOWLEDGE_BASE: {json.dumps(kb_compact, ensure_ascii=False)}\n\n"
        "Linee guida:\n"
        "- Non elencare info non richieste.\n"
        "- Se l'utente chiede orari o accesso, usa i dati della KB e includi il link al video se esiste per la struttura.\n"
        "- Transfer: tariffe KB (Aeroporto↔Città 50€, Città↔Città 40€); se utente fornisce già partenza/destinazione/orario/persone, non richiedere di nuovo. Poi chiedi solo conferma finale.\n"
        "- Parcheggio: se non è chiara la struttura, chiedi solo quella informazione e poi rispondi con il parcheggio specifico.\n"
        "- Corrente: se l'utente chiede ripristino a Casa Monic, fornisci anche il link al video se presente.\n"
        "- Evita di resettare la conversazione; non ripartire con domande generiche se hai già i dati.\n"
    )
    return sys

def call_llm(system_prompt, user_text):
    resp = openai.ChatCompletion.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
    )
    return resp["choices"][0]["message"]["content"].strip()

def reply_text(text):
    # usato sia per Twilio (TwiML) che per test web
    tw = MessagingResponse()
    tw.message(text)
    return str(tw)

@app.route("/", methods=["GET"])
def root():
    return "OK"

@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logging.debug("Inbound da %s: %s", sender, body)

    ctx = _load_or_make_context(sender, body)
    system_prompt = build_system_prompt(ctx)
    answer = call_llm(system_prompt, body)

    return Response(reply_text(answer), mimetype="application/xml")

# ========== Pagina di test ==========
TEST_HTML = """
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Bot Test</title>
<style>
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin:0; background:#f6f7f9; }
.container { max-width: 900px; margin: 0 auto; padding: 24px; }
.card { background:#fff; border-radius:16px; box-shadow:0 2px 12px rgba(0,0,0,.06); padding:20px; }
h1 { margin:0 0 8px; font-size:20px; }
small{ color:#666; }
.row { display:flex; gap:10px; margin: 12px 0; }
input, textarea, button {
  font-size:16px; padding:10px 12px; border:1px solid #e3e5ea; border-radius:10px;
}
input[type=text]{ flex:1; }
textarea{ width:100%; height:74px; resize:vertical; }
button { background:#111827; color:#fff; border:none; cursor:pointer; border-radius:10px; }
button:disabled{ opacity:.5; }
.chat { background:#fafafa; border:1px solid #eee; border-radius:14px; padding:14px; height:360px; overflow:auto; }
.msg { margin:8px 0; }
.me { text-align:right; }
.me .bubble { display:inline-block; background:#0ea5e9; color:#fff; padding:8px 12px; border-radius:12px 12px 0 12px; }
.bot .bubble { display:inline-block; background:#e5e7eb; color:#111; padding:8px 12px; border-radius:12px 12px 12px 0; }
.sys { text-align:center; color:#666; font-size:12px; }
footer { color:#777; margin-top:12px; font-size:12px; }
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>Test WhatsApp Bot (Render)</h1>
    <small>Simula messaggi verso <code>/webhook</code> senza Twilio.</small>
    <div class="row">
      <input id="phone" type="text" placeholder="Telefono (es. 39347...)" />
      <button id="start">Avvia</button>
    </div>
    <div class="chat" id="chat"></div>
    <div class="row">
      <textarea id="msg" placeholder="Scrivi un messaggio..."></textarea>
      <button id="send">Invia</button>
    </div>
    <footer>La cronologia resta in pagina finché non ricarichi.</footer>
  </div>
</div>
<script>
const chat = document.getElementById('chat');
const phoneInput = document.getElementById('phone');
const startBtn = document.getElementById('start');
const sendBtn = document.getElementById('send');
const msgInput = document.getElementById('msg');

function addMsg(who, text){
  const d = document.createElement('div');
  d.className = 'msg ' + who;
  if(who==='sys'){ d.innerHTML = '<div class="sys">'+text+'</div>'; chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return; }
  const b = document.createElement('span');
  b.className = 'bubble';
  b.textContent = text;
  d.appendChild(b);
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}

startBtn.onclick = () => {
  if(!phoneInput.value.trim()){ alert('Inserisci un telefono'); return; }
  addMsg('sys','Conversazione pronta. Inserisci messaggi sotto.');
};

sendBtn.onclick = async () => {
  const phone = phoneInput.value.trim();
  const text = msgInput.value.trim();
  if(!phone || !text) return;
  addMsg('me', text);
  msgInput.value = '';
  const fd = new FormData();
  fd.append("From", "whatsapp:+" + phone);
  fd.append("Body", text);
  const r = await fetch('/webhook', { method: 'POST', body: fd });
  const xml = await r.text();
  // Estrai testo dalla TwiML <Message>
  const m = xml.match(/<Message>([\\s\\S]*?)<\\/Message>/);
  const reply = m ? m[1] : '(nessuna risposta)';
  addMsg('bot', reply);
};
</script>
</body>
</html>
"""

@app.route("/test", methods=["GET"])
def test_page():
    return Response(TEST_HTML, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
