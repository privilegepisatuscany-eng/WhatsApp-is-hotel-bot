import re
from typing import List, Dict

def normalize_sender(raw: str) -> str:
    # Twilio format: "whatsapp:+39347..." -> "39347..."
    s = raw or ""
    s = s.replace("whatsapp:", "").replace("+", "").strip()
    # Tieni solo numeri
    digits = re.sub(r"\D+", "", s)
    return digits

def clamp_history(history: List[Dict[str, str]], max_pairs: int = 6) -> List[Dict[str, str]]:
    # max_pairs coppie (user, assistant)
    # normalizza: mantieni solo gli ultimi 2*max_pairs messaggi
    if not history:
        return []
    return history[-max_pairs*2:]

_RES_ID_RE = re.compile(r"\b(\d{6,})\b")

def extract_reservation_id(text: str) -> str:
    m = _RES_ID_RE.search(text or "")
    return m.group(1) if m else ""
