import os
import time
import logging
import requests

BASE = os.environ.get("CIAOBOOKING_BASE", "https://api.ciaobooking.com")
EMAIL = os.environ.get("CIAOBOOKING_EMAIL", "")
PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD", "")
SOURCE = os.environ.get("CIAOBOOKING_SOURCE", "bot")

_session = requests.Session()
_token_cache = {"token": None, "exp": 0}

def cb_login():
    now = int(time.time())
    if _token_cache["token"] and _token_cache["exp"] - now > 60:
        return _token_cache["token"]
    try:
        r = _session.post(
            f"{BASE}/api/public/login",
            files={
                "email": (None, EMAIL),
                "password": (None, PASSWORD),
                "source": (None, SOURCE),
            },
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        token = data.get("token")
        exp = data.get("expiresAt", now + 3600)
        if token:
            _token_cache["token"] = token
            _token_cache["exp"] = exp
            logging.info("CiaoBooking login OK; token valid until %s", exp)
            return token
    except requests.RequestException as e:
        logging.error("CiaoBooking login error: %s", e)
    return None

def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}

def find_client_by_phone(phone: str, token: str):
    """
    GET /api/public/clients/paginated?search=<phone_normalized>
    phone_normalized deve essere senza + e spazi
    """
    try:
        normalized = phone.replace("+", "").replace("whatsapp:", "").strip()
        params = {
            "limit": "5",
            "page": "1",
            "search": normalized,
            "order": "asc",
            "sortBy[]": "name",
        }
        r = _session.get(
            f"{BASE}/api/public/clients/paginated",
            headers=_auth_headers(token),
            params=params,
            timeout=5,
        )
        r.raise_for_status()
        col = (r.json().get("data") or {}).get("collection") or []
        return col[0] if col else None
    except requests.HTTPError as he:
        try:
            j = he.response.json()
            logging.error("CiaoBooking error: %s", j)
        except Exception:
            logging.error("CiaoBooking error: %s", he)
        return None
    except requests.RequestException as e:
        logging.error("CiaoBooking request error: %s", e)
        return None

def find_reservation_by_ref(ref: str, token: str):
    """ GET /api/public/reservations/{id} """
    try:
        r = _session.get(
            f"{BASE}/api/public/reservations/{ref}",
            headers=_auth_headers(token),
            timeout=5,
        )
        if r.status_code == 404:
            logging.error("CiaoBooking reservation error: %s", r.text)
            return None
        r.raise_for_status()
        return (r.json() or {}).get("data")
    except requests.RequestException as e:
        logging.error("CiaoBooking reservation request error: %s", e)
        return None
