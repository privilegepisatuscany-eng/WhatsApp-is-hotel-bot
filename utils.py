import re

def normalize_sender(raw: str) -> str:
    """
    Normalizza il mittente:
    - rimuove 'whatsapp:' e '+' e leading zero del prefisso internazionale
    - mantiene solo cifre
    """
    s = (raw or "").strip()
    s = s.replace("whatsapp:", "").replace("+", "")
    # solo cifre
    s = re.sub(r"\D", "", s)
    return s

def clamp_history(history: list[dict], max_turns: int = 12) -> list[dict]:
    """Limita la history a ~max_turns messaggi (user+assistant) * 2."""
    if len(history) > max_turns:
        return history[-max_turns:]
    return history
