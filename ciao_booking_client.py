import os
import time
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATUS_MAP = {1: "CANCELED", 2: "CONFIRMED", 3: "PENDING"}
GUEST_STATUS_MAP = {0: "NOT_ARRIVED", 1: "ARRIVED", 2: "LEFT"}
CHECKIN_MAP = {0: "TO_DO", 1: "COMPLETED", 2: "VERIFIED"}

class CiaoBookingClient:
    def __init__(self, base_url, email, password, locale="it"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale
        self._token = None
        self._exp = 0

    # ----------------- Auth -----------------
    def _login(self):
        now = int(time.time())
        if self._token and now < self._exp - 60:
            return self._token

        url = f"{self.base_url}/api/public/login"
        payload = {"email": self.email, "password": self.password, "locale": self.locale}
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json().get("data") or {}
        self._token = data.get("access_token")
        self._exp = data.get("expires_at", now + 3600)
        logger.info("CiaoBooking login OK; token valid until %s", self._exp)
        return self._token

    def _headers(self):
        token = self._login()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ----------------- Low-level helpers -----------------
    def find_client_by_phone(self, phone: str):
        """Restituisce il primo client che matcha il numero (stringa numerica, senza +)."""
        phone = (phone or "").replace("+", "").strip()
        if not phone:
            return None

        url = f"{self.base_url}/api/public/clients/paginated"
        params = {
            "limit": 5,
            "page": 1,
            "search": phone,
            "order": "asc",
            "sortBy[]": "name",
        }
        r = requests.get(url, headers=self._headers(), params=params, timeout=10)
        if r.status_code == 404:
            logger.info("CiaoBooking: client non trovato (%s)", phone)
            return None
        r.raise_for_status()
        data = r.json().get("data", {})
        coll = data.get("collection") or []
        if not coll:
            logger.info("CiaoBooking: client non trovato (%s)", phone)
            return None
        return coll[0]

    def get_reservation_by_id(self, reservation_id: str | int):
        """GET /api/public/reservations/{id}"""
        if not reservation_id:
            return None
        rid = str(reservation_id).strip()
        url = f"{self.base_url}/api/public/reservations/{rid}"
        r = requests.get(url, headers=self._headers(), timeout=10)
        if r.status_code == 404:
            logger.error("CiaoBooking reservation error: %s", r.text)
            return None
        r.raise_for_status()
        return r.json().get("data") or None

    # ----------------- High level: booking context -----------------
    def get_booking_context(self, *, phone: str | None = None, reservation_id: str | int | None = None):
        """
        Ritorna un dizionario 'booking_ctx' usabile dal bot:
        {
          'client': {...} | None,
          'reservation': {...} | None,
          'status_text': 'CONFIRMED|PENDING|CANCELED' | None,
          'guest_status_text': 'NOT_ARRIVED|ARRIVED|LEFT' | None,
          'checkin_text': 'TO_DO|COMPLETED|VERIFIED' | None,
          'property_name': str | None,
          'property_id': int | None,
          'room_type_id': int | None,
          'unit_id': int | None,
          'start_date': 'YYYY-MM-DD' | None,
          'end_date': 'YYYY-MM-DD' | None
        }
        """
        ctx = {
            "client": None,
            "reservation": None,
            "status_text": None,
            "guest_status_text": None,
            "checkin_text": None,
            "property_name": None,
            "property_id": None,
            "room_type_id": None,
            "unit_id": None,
            "start_date": None,
            "end_date": None,
        }

        # 1) Se ho il telefono, provo a reperire il client
        if phone:
            try:
                client = self.find_client_by_phone(phone)
                if client:
                    ctx["client"] = client
            except Exception as e:
                logger.error("Errore lookup CiaoBooking (phone): %s", e)

        # 2) Se ho reservation_id, prendo i dettagli
        if reservation_id:
            try:
                res = self.get_reservation_by_id(reservation_id)
                if res:
                    ctx["reservation"] = res
                    ctx["status_text"] = STATUS_MAP.get(res.get("status"))
                    ctx["guest_status_text"] = GUEST_STATUS_MAP.get(res.get("guest_status"))
                    ctx["checkin_text"] = CHECKIN_MAP.get(res.get("is_checkin_completed"))

                    prop = res.get("property") or {}
                    ctx["property_name"] = prop.get("name")
                    ctx["property_id"] = res.get("property_id")
                    ctx["room_type_id"] = res.get("room_type_id")
                    ctx["unit_id"] = res.get("unit_id")
                    ctx["start_date"] = res.get("start_date")
                    ctx["end_date"] = res.get("end_date")
            except Exception as e:
                logger.error("Errore lookup CiaoBooking (reservation_id): %s", e)

        return ctx
