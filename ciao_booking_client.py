import time
import logging
import requests

logger = logging.getLogger(__name__)

class CiaoBookingClient:
    def __init__(self, base_url: str, email: str, password: str, locale: str = "it"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale
        self._token = None
        self._exp = 0

    def _login(self):
        url = f"{self.base_url}/api/public/login"
        files = {
            "email": (None, self.email),
            "password": (None, self.password),
            "source": (None, "api"),
        }
        headers = {"Accept": "application/json"}
        r = requests.post(url, files=files, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        self._token = data.get("token")
        self._exp = data.get("expiresAt", int(time.time()) + 60*60*12)
        logger.info("CiaoBooking login OK; token valid until %s", self._exp)

    def _headers(self):
        now = int(time.time())
        if not self._token or now >= (self._exp - 120):
            self._login()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def find_client_by_phone(self, phone_normalized: str):
        """GET /api/public/clients/paginated?search=..."""
        url = f"{self.base_url}/api/public/clients/paginated"
        params = {
            "limit": "5",
            "page": "1",
            "search": phone_normalized,
            "order": "asc",
            "sortBy[]": "name",
        }
        r = requests.get(url, headers=self._headers(), params=params, timeout=10)
        if r.status_code == 200:
            js = r.json()
            coll = js.get("data", {}).get("collection", [])
            return coll[0] if coll else None
        logger.error("CiaoBooking error clients/paginated: %s", r.text)
        r.raise_for_status()

    def get_reservation_by_id(self, reservation_id: str):
        url = f"{self.base_url}/api/public/reservations/{reservation_id}"
        r = requests.get(url, headers=self._headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("data")
        logger.error("CiaoBooking reservation error: %s", r.text)
        r.raise_for_status()

    def get_booking_context_by_phone(self, phone_normalized: str):
        """Ritorna contesto minimale (nome cliente, struttura stimata se disponibile)."""
        try:
            cli = self.find_client_by_phone(phone_normalized)
        except requests.HTTPError as e:
            logger.error("CiaoBooking error: %s", e)
            return None

        if not cli:
            return None

        ctx = {
            "client_name": cli.get("name"),
            "reservation_id": None,
            "property": None,
            "start_date": None,
            "end_date": None,
            "docs_status": None,  # Non esposto chiaramente dall’API pubblica
        }
        # Se in futuro colleghi cliente → prenotazioni, popola qui.
        return ctx
