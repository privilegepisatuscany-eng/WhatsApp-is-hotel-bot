import os, logging, time, requests
from typing import Dict, Any, Optional

BASE_URL = "https://api.ciaobooking.com"
EMAIL = os.environ.get("CIAOBOOKING_EMAIL", "")
PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD", "")
LOCALE = os.environ.get("CIAOBOOKING_LOCALE", "it")

_session: Dict[str, Any] = {"token": None, "exp": 0}

def _login_if_needed():
    now = int(time.time())
    if _session["token"] and _session["exp"] > now + 60:
        return
    url = f"{BASE_URL}/api/public/login"
    resp = requests.post(url, data={"email": EMAIL, "password": PASSWORD, "source": "bot"}, headers={"Accept-Language": LOCALE}, timeout=8)
    resp.raise_for_status()
    data = resp.json()["data"]
    _session["token"] = data["token"]
    _session["exp"] = data["expiresAt"]
    logging.info("CiaoBooking login OK; token valid until %s", _session["exp"])

def _headers():
    _login_if_needed()
    return {
        "Authorization": f"Bearer {_session['token']}",
        "Accept-Language": LOCALE,
        "Content-Type": "application/json",
    }

def first_touch_ciaobooking_lookup(phone_normalized: str, sess: Dict[str, Any]):
    """
    Prova a trovare il client per numero di telefono (GET paginated con search)
    e salva un contesto minimale in sessione.
    """
    try:
        url = f"{BASE_URL}/api/public/clients/paginated"
        params = {
            "limit": "5",
            "page": "1",
            "search": phone_normalized,
            "order": "asc",
            "sortBy[]": "name"
        }
        r = requests.get(url, headers=_headers(), params=params, timeout=8)
        if r.status_code >= 400:
            logging.error("CiaoBooking error: %s", r.text)
            r.raise_for_status()
        data = r.json().get("data", {}).get("collection", [])
        if not data:
            logging.info("CiaoBooking: client non trovato (%s)", phone_normalized)
            sess["ciaobooking_client"] = None
        else:
            sess["ciaobooking_client"] = data[0]
            logging.info("CiaoBooking: client trovato id=%s", data[0].get("id"))
    except Exception as e:
        logging.error("Errore lookup CiaoBooking: %s", e)
        sess["ciaobooking_client"] = None

def get_reservation_by_id(res_id: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"{BASE_URL}/api/public/reservations/{res_id}"
        r = requests.get(url, headers=_headers(), timeout=8)
        if r.status_code == 404:
            logging.error("CiaoBooking reservation error: %s", r.text)
            return None
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        logging.error("Errore reservation CiaoBooking: %s", e)
        return None
