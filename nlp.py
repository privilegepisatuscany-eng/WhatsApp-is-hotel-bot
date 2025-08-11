
import re

STRUCTURE_ALIASES = {
    "relais": "Relais dell’Ussero",
    "ussero": "Relais dell’Ussero",
    "casa monic": "Casa Monic",
    "monic": "Casa Monic",
    "belle vue": "Belle Vue",
    "bellevue": "Belle Vue",
    "villino": "Villino di Monic",
    "villino di monic": "Villino di Monic",
    "gina": "Casa di Gina",
    "casa di gina": "Casa di Gina",
}

INTENT_PATTERNS = {
    "transfer": r"\b(taxi|transfer|trasfer|aeroporto|airport|stazione)\b",
    "parking": r"\b(parchegg|auto|macchina|park)\b",
    "power": r"\b(corrente|luce|contatore|salta(ta)? la corrente|blackout)\b",
    "checkin_info": r"\b(check ?in|check-in|istruzioni|video)\b",
    "greeting": r"\b(ciao|buongiorno|salve|hello|hi)\b",
}

def detect_intent(text: str) -> str:
    t = text.lower()
    for intent, pat in INTENT_PATTERNS.items():
        if re.search(pat, t):
            return intent
    return "other"

def extract_slots(text: str) -> dict:
    t = text.lower()
    slots = {}
    # time HH:MM or H.MM
    m = re.search(r"\b(\d{1,2})[:\.](\d{2})\b", t)
    if m:
        slots["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    # people count
    m2 = re.search(r"\b([1-9]|1[0-9])\s*(persone|people|ospiti)?\b", t)
    if m2:
        slots["people"] = int(m2.group(1))
    # origin/destination heuristics
    if "aeroporto" in t or "airport" in t:
        if "da" in t or "dall" in t or "dall’" in t or "dall'" in t:
            slots["origin"] = "aeroporto"
        if "a" in t or "verso" in t or "destinazione" in t:
            slots["destination"] = "aeroporto"
        # fallback: if asks “dall’aeroporto” we assume origin=aeroporto
        if "dall" in t or "dall’" in t or "dall'" in t:
            slots["origin"] = "aeroporto"
    # structure
    for alias, canonical in STRUCTURE_ALIASES.items():
        if alias in t:
            slots["structure"] = canonical
            break
    return slots
