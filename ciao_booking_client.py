import time
import json
import logging
import requests

class CiaoBookingClient:
    def __init__(self, base_url, static_token=None, email=None, password=None, source="whatsapp-bot", locale="it"):
        self.base_url = base_url.rstrip("/")
        self.static_token = static_token
        self.email = email
        self.password = password
        self.source = source
        self.locale = locale

        self._token = None
        self._token_exp = 0

    # -------------------------------
    # Auth
    # -------------------------------
    def _auth_header(self):
        token = self._get_token()
        return {"Authorization": f"Bearer {token}"}

    def _get_token(self):
        # Se c'è un token “statico”, usalo
        if self.static_token:
            return self.static_token

        # Se abbiamo token valido in cache, riutilizzalo
        now = int(time.time())
        if self._token and now < self._token_exp - 60:
            return self._token

        if not self.email or not self.password:
            raise RuntimeError("CiaoBooking: credenziali non impostate e CIAOBOOKING_TOKEN assente.")

        # Login
        url = f"{self.base_url}/api/public/login"
        files = {
            "email": (None, self.email),
            "password": (None, self.password),
            "source": (None, self.source),
        }
        headers = {}
        if self.locale:
            headers["locale"] = self.locale

        r = requests.post(url, files=files, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        token = data.get("token")
        exp = data.get("expiresAt")
        if not token:
            raise RuntimeError("CiaoBooking: login OK ma token mancante.")
        self._token = token
        self._token_exp = int(exp) if exp else (int(time.time()) + 3600)
        logging.info("CiaoBooking login OK; token valid until %s", self._token_exp)
        return self._token

    # -------------------------------
    # Lookup cliente per telefono
    # -------------------------------
    def get_booking_context_by_phone(self, phone_number_normalized):
        """
        Ritorna:
        {
          "has_client": bool,
          "client": {...} (se presente),
        }
        Usa /api/public/clients/paginated (POST) con campo 'search'.
        """
        try:
            url = f"{self.base_url}/api/public/clients/paginated"
            payload = {
                "limit": 5,
                "page": 1,
                "search": phone_number_normalized
            }
            headers = {
                "Content-Type": "application/json",
                **self._auth_header()
            }
            r = requests.post(url, data=json.dumps(payload), headers=headers, timeout=10)
            r.raise_for_status()
            js = r.json()
            coll = (js.get("data") or {}).get("collection") or []
            if not coll:
                logging.info("CiaoBooking client NOT FOUND")
                return {"has_client": False}
            client = coll[0]
            return {"has_client": True, "client": client}
        except requests.HTTPError as e:
            try:
                js = e.response.json()
                logging.error("CiaoBooking error: %s", json.dumps(js, ensure_ascii=False))
            except Exception:
                logging.error("CiaoBooking HTTP error: %s", e)
            raise
        except Exception as e:
            logging.error("CiaoBooking generic error: %s", e)
            raise
