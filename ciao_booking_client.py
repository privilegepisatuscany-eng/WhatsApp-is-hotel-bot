
import os
import time
import logging
import requests

BASE_URL = os.environ.get("CIAOBOOKING_BASE_URL", "https://api.ciaobooking.com")
EMAIL = os.environ.get("CIAOBOOKING_EMAIL", "")
PASSWORD = os.environ.get("CIAOBOOKING_PASSWORD", "")
SOURCE = os.environ.get("CIAOBOOKING_SOURCE", "bot")

_token = None
_token_exp = 0

def _login():
    global _token, _token_exp
    if not EMAIL or not PASSWORD:
        logging.info("CiaoBooking login skipped: credentials not set")
        return False
    if _token and _token_exp > time.time() + 60:
        return True
    url = f"{BASE_URL}/api/public/login"
    try:
        r = requests.post(url, files={
            "email": (None, EMAIL),
            "password": (None, PASSWORD),
            "source": (None, SOURCE),
        }, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        _token = data.get("token")
        _token_exp = data.get("expiresAt", 0)
        logging.info("CiaoBooking login OK; token valid until %s", _token_exp)
        return True
    except Exception as e:
        logging.error("CiaoBooking login error: %s", e)
        _token = None
        _token_exp = 0
        return False

def _auth_headers():
    return {"Authorization": f"Bearer {_token}"} if _token else {}

def normalize_phone(p: str) -> str:
    return "".join(ch for ch in p if ch.isdigit())

def find_client_by_phone(phone: str):
    """Search client with GET /api/public/clients/paginated?search=..."""
    if not _login():
        return None
    norm = normalize_phone(phone)
    url = f"{BASE_URL}/api/public/clients/paginated"
    try:
        r = requests.get(url, params={"limit": "5", "page": "1", "search": norm},
                         headers=_auth_headers(), timeout=10)
        if r.status_code == 401:
            # token expired → retry once
            _login()
            r = requests.get(url, params={"limit": "5", "page": "1", "search": norm},
                             headers=_auth_headers(), timeout=10)
        r.raise_for_status()
        coll = r.json().get("data", {}).get("collection", [])
        if coll:
            return coll[0]
        return None
    except requests.HTTPError as he:
        try:
            js = r.json()
            logging.info("CiaoBooking lookup skipped (status %s): %s", r.status_code, js)
        except Exception:
            logging.info("CiaoBooking lookup skipped (status %s)", r.status_code)
        return None
    except Exception as e:
        logging.error("CiaoBooking error: %s", e)
        return None
