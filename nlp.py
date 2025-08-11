import re

INTENTS = {
    "transfer": re.compile(r"\b(transfer|trasfer|taxi)\b", re.I),
    "parking": re.compile(r"\b(parcheggio|parcheggiare|auto)\b", re.I),
    "video": re.compile(r"\b(video|check[\s\-]?in|istruzioni|accesso)\b", re.I),
    "power": re.compile(r"\b(corrente|luce|salta[ta]?|blackout)\b", re.I),
}

def detect_intent(text: str) -> str:
    for k, rx in INTENTS.items():
        if rx.search(text or ""):
            return k
    return "greeting"

def normalize_sender(from_field: str) -> str:
    # Twilio -> "whatsapp:+39123..."
    return (from_field or "").strip()

def fmt_euro(amount: int) -> str:
    return f"{amount}€"
