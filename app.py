import os
import re
import time
import json
import logging
from typing import Optional, Dict, Any
from flask import Flask, request, render_template, Response

from ciao_booking_client import (
    find_client_by_phone,
    get_reservation_by_id,
)

# ------------------------------------------------------------------------------------
# Logging robusto (accetta LOG_LEVEL in minuscolo, es. "debug")
# ------------------------------------------------------------------------------------
_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _level_name, logging.INFO)
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------
# Flask & memoria di sessione (in-memory; in prod -> Redis)
# ------------------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates")

SESSIONS: Dict[str, Dict[str, Any]] = {}  # key = sender normalizzato

def normalize_sender(v: str) -> str:
    v = (v or "").replace("whatsapp:", "").strip()
    return re.sub(r"\D+", "", v)

def twiml_message(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{text}</Message></Response>'

def load_kb() -> Dict[str, Any]:
    # carica knowledge_base.json se presente
    path = os.path.join(os.getcwd(), "knowledge_base.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

KB = load_kb()

def get_session(sender: str) -> Dict[str, Any]:
    s = SESSIONS.get(sender)
    if not s:
        s = {
            "created_at": time.time(),
            "has_booking": None,       # True/False/None
            "booking_client": None,    # dict client CiaoBooking
            "reservation": None,       # dict prenotazione CiaoBooking (se ID valido)
            "flow": None,              # "transfer" | "parcheggio" | "video" | None
            "transfer": {"persons": None, "time": None, "from": None, "to": None},
            "last_topic": None,        # ultimo macro‑tema servito ("transfer","parcheggio","video")
        }
        SESSIONS[sender] = s
    return s

# ------------------------------------------------------------------------------------
# CiaoBooking on first touch
# ------------------------------------------------------------------------------------
def first_touch_ciaobooking_lookup(sender: str, sess: Dict[str, Any]) -> None:
    if sess.get("has_booking") is not None:
        return
    try:
        client = find_client_by_phone(sender)
        if client:
            sess["has_booking"] = True
            sess["booking_client"] = client
            logger.info("CiaoBooking: client trovato %s (%s)", client.get("name",""), sender)
        else:
            sess["has_booking"] = False
            logger.info("CiaoBooking: client non trovato (%s)", sender)
    except Exception as e:
        sess["has_booking"] = False
        logger.exception("Lookup CiaoBooking fallito: %s", e)

# ------------------------------------------------------------------------------------
# Intent routing
# ------------------------------------------------------------------------------------
INTENT_TRANSFER = ("transfer", "taxi", "aeroporto", "airport")
INTENT_PARCHEGGIO = ("parchegg",)
INTENT_VIDEO = ("video", "self check-in", "self check in", "check-in", "check in")
INTENT_CORRENTE = ("corrente", "luce", "elettric", "blackout")

def detect_intent(text: str) -> Optional[str]:
    t = text.lower()
    if any(k in t for k in INTENT_TRANSFER):
        return "transfer"
    if any(k in t for k in INTENT_PARCHEGGIO):
        return "parcheggio"
    if any(k in t for k in INTENT_VIDEO) or any(k in t for k in INTENT_CORRENTE):
        return "video"
    return None

# ------------------------------------------------------------------------------------
# Formatter di messaggi di apertura
# ------------------------------------------------------------------------------------
def start_prompt(sess: Dict[str, Any]) -> str:
    if sess.get("has_booking"):
        return "Ciao! Ho trovato la tua prenotazione. Di cosa hai bisogno? *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*?"
    return "Ciao! Hai già una prenotazione? In ogni caso posso aiutarti con *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*."

# ------------------------------------------------------------------------------------
# Transfer: parser rapido “tutto in una frase”
# ------------------------------------------------------------------------------------
RE_PEOPLE = re.compile(r"(\d+)\s*(persone|pax)?", re.I)
RE_TIME = re.compile(r"\b(\d{1,2}[:.\-]?\d{0,2})\b")  # 1530 / 15:30 / 15.30 / 15-30
def normalize_time(raw: str) -> str:
    s = raw.replace(".", ":").replace("-", ":")
    if ":" not in s and len(s) in (3,4):
        # 930 -> 9:30 ; 1530 -> 15:30
        hh = s[:-2]
        mm = s[-2:]
        return f"{int(hh):02d}:{int(mm):02d}"
    if ":" in s:
        hh, mm = s.split(":")[0], s.split(":")[1] if len(s.split(":"))>1 else "00"
        return f"{int(hh):02d}:{int(mm):02d}"
    return s

def infer_from_to(text: str) -> (Optional[str], Optional[str]):
    t = text.lower()
    src = None
    dst = None
    if "aeroporto" in t or "airport" in t:
        # proviamo a indovinare direzione
        if " a casa" in t or " a " in t:
            # se compare "a casa"/"a " + una struttura, assumiamo partenza Aeroporto
            src = "Aeroporto"
        if "dall'aeroporto" in t or "da aeroporto" in t or "dall airport" in t or "dall’" in t:
            src = "Aeroporto"
        if "all’aeroporto" in t or "all'aeroporto" in t or "per aeroporto" in t:
            dst = "Aeroporto"
    # destinazioni note (strutture)
    known = ("casa monic", "monic", "belle vue", "relais", "ussero", "casa di gina", "villino")
    for k in known:
        if k in t:
            # se c'è già src Aeroporto, allora è to
            if src == "Aeroporto":
                dst = k.title()
            else:
                # altrimenti lo mettiamo come 'to' e vediamo se src emerge dopo
                dst = k.title()
    return src, dst

def transfer_step(body: str, sess: Dict[str, Any]) -> str:
    T = sess["transfer"]
    b = body.strip()

    # Fast parse: “3 persone alle 15.30 dall’aeroporto a Casa Monic”
    if all(v is None for v in T.values()):
        m_p = RE_PEOPLE.search(b)
        m_t = RE_TIME.search(b)
        src, dst = infer_from_to(b)
        if m_p:
            T["persons"] = int(m_p.group(1))
        if m_t:
            T["time"] = normalize_time(m_t.group(1))
        if src:
            T["from"] = "Aeroporto"
        # se contiene “da … a …”
        if " da " in b.lower() and " a " in b.lower():
            # molto semplice: prendi chunk dopo "da" e dopo "a"
            low = b.lower()
            p_da = low.find(" da ")
            p_a = low.find(" a ", p_da+1)
            if p_da >= 0 and p_a > p_da:
                T["from"] = b[p_da+4:p_a].strip().title()
                T["to"] = b[p_a+3:].strip().title()
        # fallback per “a casa monic”
        if not T["to"] and dst:
            T["to"] = dst

        # se tutto risolto qui, vai a conferma
        if all(T[k] for k in ("persons","time","from","to")):
            return transfer_confirm(sess)

    # domande passo‑passo
    if T["persons"] is None:
        m = RE_PEOPLE.search(b)
        if m:
            T["persons"] = int(m.group(1))
        else:
            return "Per il transfer, quante persone siete?"

    if T["time"] is None:
        m_t = RE_TIME.search(b)
        if m_t:
            T["time"] = normalize_time(m_t.group(1))
        else:
            return "A che ora desideri la partenza?"

    if T["from"] is None:
        # indovina Aeroporto se presente
        if "aeroporto" in b.lower() or "airport" in b.lower():
            T["from"] = "Aeroporto"
        else:
            return "Da dove partite?"

    if T["to"] is None:
        # prova a riconoscere una struttura
        if any(k in b.lower() for k in ("monic","belle","relais","ussero","gina","villino")):
            T["to"] = body.strip().title()
        else:
            return "Qual è la destinazione?"

    # se arriva qui, tutto c’è: chiedi conferma
    return transfer_confirm(sess)

def transfer_confirm(sess: Dict[str, Any]) -> str:
    T = sess["transfer"]
    f = (T["from"] or "").lower()
    t = (T["to"] or "").lower()
    tariffa = "50€" if ("aeroporto" in f or "airport" in f or "aeroporto" in t or "airport" in t) else "40€"
    sess["transfer"]["_tariffa"] = tariffa
    sess["last_topic"] = "transfer"
    return (f"Perfetto, riepilogo:\n"
            f"• Persone: {T['persons']}\n"
            f"• Orario: {T['time']}\n"
            f"• Partenza: {T['from']}\n"
            f"• Destinazione: {T['to']}\n\n"
            f"Tariffa: {tariffa}.\n"
            f"Confermi che va bene? (sì/no)")

def transfer_handle_confirmation(body: str, sess: Dict[str, Any]) -> Optional[str]:
    b = body.lower().strip()
    if b in ("si","sì","ok","va bene","confermo","yes","y"):
        tariffa = sess["transfer"].get("_tariffa","")
        # non resettiamo la conversazione; chiudiamo il flusso transfer
        sess["flow"] = None
        msg = (f"👍 Perfetto, confermato a {tariffa}. "
               "Niccolò ti contatterà a breve per la conferma definitiva.\n"
               "Posso aiutarti con altro? *Parcheggio* o *Video/Info accesso*.")
        return msg
    if b in ("no","non ancora","annulla","cambia"):
        sess["transfer"] = {"persons": None, "time": None, "from": None, "to": None}
        sess["flow"] = "transfer"
        return "Ok, nessun problema. Ricominciamo: quante persone siete?"
    return None

# ------------------------------------------------------------------------------------
# Parcheggio
# ------------------------------------------------------------------------------------
def parcheggio_step(body: str, sess: Dict[str, Any]) -> str:
    s = body.lower()
    sess["last_topic"] = "parcheggio"
    if "relais" in s or "ussero" in s:
        return "Relais dell’Ussero: parcheggio pubblico in Piazza Carrara (~1,50€/h), a pochi metri dal Relais."
    if "casa monic" in s or ( "monic" in s and "villino" not in s ):
        return "Casa Monic: parcheggio pubblico in Piazza Carrara o Piazza Santa Caterina (~400 m), ~1,50€/h."
    if "belle vue" in s or "rosmini" in s:
        return ("Belle Vue: via Antonio Rosmini o via Pasquale Galluppi (pagamento 08:00–14:00, poi gratis). "
                "Parcheggio custodito H24 in via Piave (a pagamento).")
    if "villino" in s:
        return ("Villino di Monic: via Vincenzo Gioberti (strisce blu lato destro) o via del Bastione. "
                "Pagamento 08:00–14:00, poi gratis.")
    if "gina" in s:
        return ("Casa di Gina: via Crispi, Piazza Aurelio Saffi o Lungarno Sidney Sonnino (~1,50€/h).")
    return ("Per consigli di parcheggio, in quale struttura alloggi?\n"
            "- Relais dell’Ussero\n- Casa Monic\n- Belle Vue\n- Villino di Monic\n- Casa di Gina")

# ------------------------------------------------------------------------------------
# Video / info accesso (incluso ripristino corrente Casa Monic)
# ------------------------------------------------------------------------------------
def kb_video_for(structure: str) -> Optional[str]:
    videos = KB.get("videos", {})
    # mappa fuzzy
    s = structure.lower()
    for key in videos.keys():
        if key.lower() in s or s in key.lower():
            return videos[key]
    return None

def video_step(body: str, sess: Dict[str, Any]) -> str:
    txt = body.lower()
    sess["last_topic"] = "video"

    # caso corrente Casa Monic (istruzioni + video)
    if any(k in txt for k in INTENT_CORRENTE):
        # se menziona Casa Monic…
        if "monic" in txt and "villino" not in txt:
            instr = KB.get("emergenze", {}).get("casa_monic_corrente", "")
            vid = KB.get("videos", {}).get("Casa Monic (ripristino corrente)")
            parts = []
            if instr:
                parts.append(instr)
            if vid:
                parts.append(f"Video: {vid}")
            if not parts:
                parts.append("Per il ripristino corrente a Casa Monic: verifica il quadro in cucina; se non torna, controlla il quadro generale accanto al portone verde (cassetta della posta, contatore COSCI).")
            parts.append("Serve altro? Posso aiutarti anche con *Parcheggio* o *Transfer*.")
            return "\n".join(parts)

    # self check‑in video per struttura citata
    wanted = None
    for name in ("relais","ussero","casa monic","monic","belle vue","gina","villino"):
        if name in txt:
            wanted = name
            break

    if wanted:
        lookup = {
            "relais": "Relais dell’Ussero",
            "ussero": "Relais dell’Ussero",
            "casa monic": "Casa Monic",
            "monic": "Casa Monic",
            "belle vue": "Belle Vue",
            "gina": "Casa di Gina",
            "villino": "Villino di Monic",
        }[wanted]
        url = kb_video_for(lookup)
        if url:
            return f"Ecco il video di self check‑in per *{lookup}*: {url}\nHai bisogno di altro?"
        return "Per questa struttura non ho un video associato. Ti serve altro?"

    # se non capiamo la struttura, chiediamola
    return ("Hai bisogno del *video di self check‑in*? Dimmi la struttura:\n"
            "- Relais dell’Ussero\n- Casa Monic\n- Belle Vue\n- Villino di Monic\n- Casa di Gina\n"
            "Oppure scrivi *corrente Casa Monic* per il ripristino luce.")

# ------------------------------------------------------------------------------------
# Webhook
# ------------------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    sender_raw = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    sender = normalize_sender(sender_raw)
    logger.debug("Inbound da %s: %s", sender, body)

    if not sender:
        return Response(twiml_message("Numero mittente mancante."), mimetype="application/xml")

    # reset conversazione
    if body.lower() == "/reset":
        if sender in SESSIONS:
            del SESSIONS[sender]
        return Response(twiml_message("✅ Conversazione resettata. Come posso aiutarti? (Taxi/Transfer, Parcheggio, Video/Info)"), mimetype="application/xml")

    sess = get_session(sender)
    first_touch_ciaobooking_lookup(sender, sess)

    # Se utente fornisce un “numero prenotazione”, proviamo lookup /reservations/{id}
    if re.search(r"\b(prenotazione|reservation|res)\b", body.lower()):
        rid = re.findall(r"\b(\d{6,})\b", body)  # ID numerico “lungo”
        if rid:
            res = get_reservation_by_id(rid[0])
            if res:
                sess["reservation"] = res
                return Response(twiml_message("Ho trovato la tua prenotazione, grazie. Come posso aiutarti? *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*?"), mimetype="application/xml")
            # se non trovata, non blocchiamo: continuiamo normale

    # Se non c’è un flow attivo, prova a rilevare intent
    if not sess.get("flow"):
        intent = detect_intent(body)
        if intent == "transfer":
            sess["flow"] = "transfer"
            # avvio transfer: proviamo parse veloce
            msg = transfer_step(body, sess)
            return Response(twiml_message(msg), mimetype="application/xml")
        if intent == "parcheggio":
            sess["flow"] = "parcheggio"
            return Response(twiml_message(parcheggio_step(body, sess)), mimetype="application/xml")
        if intent == "video":
            sess["flow"] = "video"
            return Response(twiml_message(video_step(body, sess)), mimetype="application/xml")
        # altrimenti prompt di ingresso
        return Response(twiml_message(start_prompt(sess)), mimetype="application/xml")

    # Flow attivo
    if sess["flow"] == "transfer":
        # prima controlla eventuale conferma
        maybe = transfer_handle_confirmation(body, sess)
        if maybe:
            return Response(twiml_message(maybe), mimetype="application/xml")
        # altrimenti continua raccolta
        return Response(twiml_message(transfer_step(body, sess)), mimetype="application/xml")

    if sess["flow"] == "parcheggio":
        return Response(twiml_message(parcheggio_step(body, sess)), mimetype="application/xml")

    if sess["flow"] == "video":
        return Response(twiml_message(video_step(body, sess)), mimetype="application/xml")

    # fallback
    return Response(twiml_message("Posso aiutarti con *Taxi/Transfer*, *Parcheggio* o *Video/Info accesso*."), mimetype="application/xml")

# ------------------------------------------------------------------------------------
# Test page & health
# ------------------------------------------------------------------------------------
@app.route("/test", methods=["GET"])
def test_page():
    return render_template("test.html")

@app.route("/", methods=["GET", "HEAD"])
def root():
    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
