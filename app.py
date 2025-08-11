# app.py
import os, json, re, logging, time
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# === Logging robusto (accetta "debug", "INFO", ecc.) ===
def _coerce_level(val: str):
    if isinstance(val, str):
        v = val.strip().upper()
        return getattr(logging, v, logging.INFO)
    return val if isinstance(val, int) else logging.INFO

logging.basicConfig(
    level=_coerce_level(os.environ.get("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# === Flask app ===
app = Flask(__name__)

# === OpenAI client ===
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY non impostata")
client = OpenAI(api_key=OPENAI_API_KEY)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# === Cache conversazioni (in-memory, per numero) ===
# Struttura: { from_number: {"started_at": ts, "booking": {...} or None, "property": str|None, "history":[{"role","text"}]} }
SESSION = {}

# === Knowledge Base JSON ===
KB_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")
try:
    with open(KB_PATH, "r", encoding="utf-8") as f:
        KB = json.load(f)
except Exception as e:
    log.error("Impossibile caricare knowledge_base.json: %s", e)
    KB = {}

# === CiaoBooking client (import soft) ===
try:
    from ciao_booking_client import login_if_needed, find_client_by_phone
    CIAOBOOKING_OK = True
except Exception as e:
    log.error("Import ciao_booking_client fallito: %s", e)
    CIAOBOOKING_OK = False
    def login_if_needed(*args, **kwargs): return None
    def find_client_by_phone(*args, **kwargs): return {"ok": False, "reason": "client module missing"}

# Utility
def normalize_phone(p: str) -> str:
    """Twilio passa 'whatsapp:+39347...' -> ritorna '39347...'"""
    if not p: return ""
    p = p.strip()
    if p.startswith("whatsapp:"):
        p = p.split(":", 1)[1]
    p = p.replace("+", "").replace(" ", "").replace("-", "")
    return p

def get_session(num: str) -> dict:
    s = SESSION.get(num)
    if not s:
        s = {"started_at": time.time(), "booking": None, "property": None, "history": []}
        SESSION[num] = s
    return s

def add_history(s, role, text):
    s["history"].append({"role": role, "text": text})
    # limita a ultime 20 interazioni
    if len(s["history"]) > 40:
        s["history"] = s["history"][-40:]

def infer_property_from_text(txt: str) -> str|None:
    # euristiche leggere
    t = txt.lower()
    mapping = {
        "relais": "Relais dell’Ussero",
        "ussero": "Relais dell’Ussero",
        "monic": "Casa Monic",
        "belle vue": "Belle Vue",
        "rosmini": "Belle Vue",
        "gina": "Casa di Gina",
        "villino": "Villino di Monic",
        "vincenzo gioberti": "Villino di Monic",
    }
    for k,v in mapping.items():
        if k in t:
            return v
    return None

def kb_property_summary(prop: str) -> str:
    locs = KB.get("locations", {})
    data = locs.get(prop)
    if not data:
        return ""
    parts = []
    if "address" in data:
        parts.append(f"Indirizzo: {data['address']}")
    info = data.get("info", [])
    if info:
        parts.append("Info utili: " + "; ".join(info))
    return "\n".join(parts)

def build_system_prompt(prop_hint: str|None, booking: dict|None) -> str:
    base = (
        "Sei un assistente virtuale per strutture ricettive. "
        "Usa SOLO le informazioni della knowledge base e i dati della prenotazione quando disponibili. "
        "Non inventare codici, pin o dettagli non presenti. "
        "Se una richiesta è fuori KB, chiedi con una domanda corta e precisa le info minime per aiutare.\n\n"
    )
    kb_dump = json.dumps(KB, ensure_ascii=False)
    ctx = []
    if prop_hint:
        ctx.append(f"Struttura probabile: {prop_hint}")
    if booking and booking.get("ok"):
        b = booking.get("data", {})
        # includiamo info utili di booking
        ctx.append(f"Dati prenotazione: status={b.get('status')} checkin={b.get('start_date')} checkout={b.get('end_date')} ospiti={b.get('guests')}")
        # se possibile property name
        prop_name = b.get("property_name") or b.get("property")
        if prop_name:
            ctx.append(f"Struttura: {prop_name}")
    context = "\n".join(ctx) if ctx else "Nessun contesto prenotazione disponibile."
    return base + "CONOSCENZA:\n" + kb_dump + "\n\nCONTESTO:\n" + context

def gpt_reply(system_prompt: str, user: str) -> str:
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content": system_prompt},
                {"role":"user","content": user}
            ],
            temperature=0.2,
            max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("OpenAI error: %s", e)
        return "Mi dispiace, si è verificato un errore tecnico. Riprova tra poco."

def first_message_bootstrap(from_num: str, text: str, s: dict):
    """
    Al primo messaggio: prova lookup su CiaoBooking UNA volta.
    Non mostra più blocco 🔒: se troviamo la prenotazione usiamo l’informazione,
    altrimenti chiediamo i dati minimi (nome o numero prenotazione) solo se serve.
    """
    if s.get("bootstrapped"):
        return
    s["bootstrapped"] = True
    prop = infer_property_from_text(text)
    if prop:
        s["property"] = prop

    # CiaoBooking lookup
    if CIAOBOOKING_OK:
        try:
            login_if_needed()
            pn = normalize_phone(from_num)
            bk = find_client_by_phone(pn)  # atteso: {"ok":True/False, "data":{...}} 
            if bk and bk.get("ok"):
                s["booking"] = bk
                # prova a inferire struttura da booking
                prop_name = (bk.get("data") or {}).get("property_name")
                if prop_name:
                    s["property"] = prop_name
                log.info("CiaoBooking: prenotazione trovata per %s", pn)
            else:
                log.info("CiaoBooking: nessuna prenotazione per %s (reason=%s)", pn, bk.get("reason") if bk else "unknown")
        except Exception as e:
            log.error("CiaoBooking lookup error: %s", e)

# === ROUTES ===
@app.get("/")
def root():
    return Response("OK", status=200, mimetype="text/plain")

@app.get("/health")
def health():
    return jsonify({"ok": True, "uptime": time.time()})

@app.get("/test")
def test_page():
    # Pagina di test con storico conversazione e textarea
    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Test Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#f7f7f8}
.container{max-width:820px;margin:20px auto;padding:16px}
.card{background:#fff;border:1px solid #e6e6e9;border-radius:12px;box-shadow:0 8px 16px rgba(0,0,0,0.04);padding:16px}
.row{display:flex;gap:8px;margin-top:12px}
input,textarea{width:100%;padding:10px;border:1px solid #d6d6db;border-radius:8px}
button{padding:10px 14px;border:0;border-radius:8px;background:#0b5cff;color:#fff;cursor:pointer}
.msg{padding:10px 12px;border-radius:10px;margin:6px 0;max-width:75%;}
.user{background:#e9f0ff;margin-left:auto}
.bot{background:#f0f0f1}
.small{color:#666;font-size:12px}
</style>
</head>
<body>
<div class="container">
  <h2>Tester WhatsApp Bot (no Twilio)</h2>
  <div class="card">
    <div class="row">
      <input id="from" placeholder="Numero (es. whatsapp:+39347...)" />
      <button onclick="reset()">Reset</button>
    </div>
    <div id="log" style="min-height:300px;margin-top:12px;"></div>
    <div class="row">
      <textarea id="msg" rows="2" placeholder="Scrivi un messaggio..."></textarea>
      <button onclick="send()">Invia</button>
    </div>
    <div class="small">I messaggi vengono inviati al webhook /webhook simulando Twilio.</div>
  </div>
</div>
<script>
async function send(){
  const from = document.getElementById('from').value || 'whatsapp:+390000000000';
  const body = document.getElementById('msg').value;
  if(!body) return;
  const form = new URLSearchParams();
  form.append('From', from);
  form.append('Body', body);
  const r = await fetch('/webhook', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:form});
  const text = await r.text();
  append('user', body);
  append('bot', text);
  document.getElementById('msg').value = '';
}
async function reset(){
  const from = document.getElementById('from').value || 'whatsapp:+390000000000';
  const form = new URLSearchParams();
  form.append('From', from);
  form.append('Body', '/reset');
  await fetch('/webhook', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:form});
  document.getElementById('log').innerHTML = '';
}
function append(who, text){
  const log = document.getElementById('log');
  const d = document.createElement('div');
  d.className = 'msg ' + (who==='user'?'user':'bot');
  d.textContent = text.replace(/<[^>]+>/g,'');
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")

@app.post("/webhook")
def whatsapp_webhook():
    from_raw = request.form.get("From", "")
    body = (request.form.get("Body", "") or "").strip()
    if not body:
        tw = MessagingResponse(); tw.message("Ciao! Come posso aiutarti?")
        return str(tw)

    s = get_session(from_raw)
    add_history(s, "user", body)

    # comandi
    if body.lower() in ("/reset","reset"):
        SESSION.pop(from_raw, None)
        tw = MessagingResponse(); tw.message("✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio o Altro)")
        return str(tw)

    # bootstrap al primo messaggio
    first_message_bootstrap(from_raw, body, s)

    # Se l’utente chiede subito “prenotazione”, NON mostriamo più 🔒.
    # Se abbiamo booking → confermiamo presenza e chiediamo il tema; se no → chiediamo nome/cognome SOLO se serve davvero.
    low = body.lower()
    if any(k in low for k in ["prenotaz", "ho una prenotazione", "booking"]):
        if s.get("booking",{}).get("ok"):
            tw = MessagingResponse()
            tw.message("Ho trovato la tua prenotazione. Come posso aiutarti? Posso darti informazioni su taxi/transfer, parcheggio, check-in o orari.")
            return str(tw)
        else:
            tw = MessagingResponse()
            tw.message("Non rilevo una prenotazione associata al tuo numero. Vuoi indicarmi il nome e cognome con cui hai prenotato per verificare?")
            return str(tw)

    # Se chiede “come posso arrivare / taxi / transfer” gestiamo form taxi con tariffa corretta
    if re.search(r"\btaxi\b|\btransfer\b|arrivar(e|ci) (all[a|o]|alla|al) (struttura|casa|relais|appartamento)|aeroporto", low):
        # quick form: se possiamo capire già aeroporto/città
        msg = []
        # Se nel testo si cita aeroporto → tariffa 50€
        if "aeroporto" in low:
            msg.append("Transfer da/per aeroporto: 50€.")
        else:
            msg.append("Transfer in città: 40€.")
        msg.append("Per procedere mi servono: orario, numero di persone e destinazione (se non l’hai già indicata).")
        tw = MessagingResponse(); tw.message("\n".join(msg))
        return str(tw)

    # Parcheggio per struttura (se nota)
    if "parchegg" in low:
        prop = s.get("property") or infer_property_from_text(body)
        if not prop:
            tw = MessagingResponse()
            tw.message("Per indicarti il parcheggio giusto, mi dici in quale struttura stai alloggiando? (Relais dell’Ussero, Casa Monic, Belle Vue, Villino di Monic, Casa di Gina)")
            return str(tw)
        # risponde con suggerimento sintetico dalla KB se presente
        loc = KB.get("locations", {}).get(prop, {})
        tip = ""
        if "Relais dell’Ussero" == prop:
            tip = "Parcheggio pubblico Piazza Carrara (1,50€/h), a pochi metri dal Relais."
        elif "Casa Monic" == prop:
            tip = "Piazza Carrara o Piazza Santa Caterina (pubblici, ~400m, 1,50€/h)."
        elif "Belle Vue" == prop:
            tip = "Sotto il palazzo in via Rosmini o in via Galluppi (a pagamento 08:00–14:00; dopo GRATIS). Custodito H24 in via Piave (a pagamento)."
        elif "Casa di Gina" == prop:
            tip = "Via Crispi, Piazza Aurelio Saffi o Lungarno Sidney Sonnino (1,50€/h)."
        elif "Villino di Monic" == prop:
            tip = "Posto auto privato incluso (se indicato in prenotazione); in alternativa strisce blu in zona."
        if not tip:
            tip = kb_property_summary(prop) or "Per il parcheggio ti do indicazioni precise appena so la struttura."
        tw = MessagingResponse(); tw.message(tip)
        return str(tw)

    # Corrente / guasti → rispondi se KB specifica (es. Casa Monic), altrimenti passa contatto
    if "corrente" in low or "luce" in low or "elettric" in low:
        prop = s.get("property") or infer_property_from_text(body)
        if prop == "Casa Monic":
            msg = ("Se la corrente è andata via:\n"
                   "• In cucina, a sinistra dell’ingresso, c’è la porta del quadro elettrico.\n"
                   "• Se non torna, controlla il quadro generale accanto al portone verde (nelle cassette della posta). "
                   "Apri con la chiave che trovi in casa, cerca il contatore 'COSCI LAURA' e alza la levetta.")
            tw = MessagingResponse(); tw.message(msg)
            return str(tw)
        else:
            tw = MessagingResponse(); tw.message("Per guasti o emergenze elettriche ti metto subito in contatto con Niccolò: +39 333 6011867.")
            return str(tw)

    # Se l’utente chiede genericamente info → usa GPT con prompt basato su KB + contesto prenotazione
    system_prompt = build_system_prompt(s.get("property"), s.get("booking"))
    ai = gpt_reply(system_prompt, body)

    add_history(s, "assistant", ai)
    tw = MessagingResponse(); tw.message(ai)
    return str(tw)

# Gunicorn entrypoint expects "app"
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
