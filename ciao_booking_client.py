import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ciaobooking.com"

EMAIL = os.environ.get("CIAOBOOKING_EMAIL", "")
PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD", "")
SOURCE = os.environ.get("CIAOBOOKING_SOURCE", "api")

_TOKEN = None
_TOKEN_EXPIRES_AT = 0

def _login():
    global _TOKEN, _TOKEN_EXPIRES_AT
    if not EMAIL or not PASSWORD:
        logger.warning("CiaoBooking credenziali mancanti (CIAOBOOKING_EMAIL/PASSWORD).")
        return
    try:
        url = f"{BASE_URL}/api/public/login"
        files = {
            "email": (None, EMAIL),
            "password": (None, PASSWORD),
            "source": (None, SOURCE),
        }
        r = requests.post(url, files=files, timeout=10)
        r.raise_for_status()
        js = r.json().get("data", {})
        _TOKEN = js.get("token")
        _TOKEN_EXPIRES_AT = int(js.get("expiresAt", 0))
        logger.info("CiaoBooking login OK; token valid until %s", _TOKEN_EXPIRES_AT)
    except Exception as e:
        logger.exception("CiaoBooking login error: %s", e)
        _TOKEN = None
        _TOKEN_EXPIRES_AT = 0

def _ensure_token():
    now = int(time.time())
    if _TOKEN and now < (_TOKEN_EXPIRES_AT - 60):
        return
    _login()

def _auth_headers():
    _ensure_token()
    if not _TOKEN:
        return {}
    return {"Authorization": f"Bearer {_TOKEN}"}

def find_client_by_phone(phone_normalized: str):
    """GET /api/public/clients/paginated?search=... (fix definitivo)."""
    try:
        headers = _auth_headers()
        if not headers:
            return None
        url = f"{BASE_URL}/api/public/clients/paginated"
        params = {
            "limit": "5",
            "page": "1",
            "search": phone_normalized,
            "order": "asc",
            "sortBy[]": "name",
        }
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code >= 400:
            logger.error("CiaoBooking error: %s", r.text)
            r.raise_for_status()
        data = r.json().get("data", {})
        coll = data.get("collection", [])
        return coll[0] if coll else None
    except Exception as e:
        logger.exception("Errore lookup CiaoBooking: %s", e)
        return None

def get_reservation_by_id(res_id: str):
    """GET /api/public/reservations/{id} se l’utente fornisce un numero prenotazione."""
    try:
        headers = _auth_headers()
        if not headers:
            return None
        url = f"{BASE_URL}/api/public/reservations/{res_id}"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code >= 400:
            logger.error("CiaoBooking reservation error: %s", r.text)
            return None
        return r.json().get("data", None)
    except Exception as e:
        logger.exception("Errore get_reservation_by_id: %s", e)
        return None
