# ciao_booking_client.py
import os
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple, List

import requests

logger = logging.getLogger("bot.ciaobooking")


INT_STATUS = {
    1: "CANCELED",
    2: "CONFIRMED",
    3: "PENDING",
}
INT_GUEST_STATUS = {
    0: "NOT_ARRIVED",
    1: "ARRIVED",
    2: "LEFT",
}
INT_CHECKIN = {
    0: "TO_DO",
    1: "COMPLETED",
    2: "VERIFIED",
}


class CiaoBookingClient:
    """
    Client leggero per API pubbliche di CiaoBooking.
    Implementa:
      - login()
      - search_clients(search)
      - list_reservations(from,to,status)
      - get_reservation_by_id(id)
      - get_booking_context(phone, reservation_id=None)
    """
    def __init__(self, base_url: str, email: str, password: str, locale: str = "it"):
        self.base_url = (base_url or "").rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale
        self.session = requests.Session()
        self.token = None
        self.token_exp = None

    # --- HTTP helpers
    def _headers(self) -> Dict[str, str]:
        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self) -> None:
        url = f"{self.base_url}/api/public/login"
        resp = self.session.post(url, json={"email": self.email, "password": self.password}, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}
        self.token = (data.get("data") or {}).get("token") or data.get("token")
        self.token_exp = (data.get("data") or {}).get("token_expires_at") or data.get("token_expires_at")
        logger.info("CiaoBooking login OK; token valid until %s", self.token_exp)

    def _ensure_login(self) -> None:
        if not self.token:
            self.login()

    # --- Public endpoints
    def search_clients(self, search: str, limit: int = 5, page: int = 1) -> List[Dict[str, Any]]:
        """
        GET /api/public/clients/paginated?limit=&page=&search=&order=asc&sortBy[]=name
        Ritorna la collection (lista) di clienti che matchano 'search' (telefono o email).
        """
        self._ensure_login()
        params = {
            "limit": str(limit),
            "page": str(page),
            "search": (search or "").strip(),
            "order": "asc",
            "sortBy[]": "name",
        }
        url = f"{self.base_url}/api/public/clients/paginated"
        r = self.session.get(url, headers=self._headers(), params=params, timeout=10)
        r.raise_for_status()
        js = r.json() or {}
        return ((js.get("data") or {}).get("collection") or [])  # sempre lista

    def list_reservations(
        self,
        date_from: str,
        date_to: str,
        status: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        property_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        GET /api/public/reservations?from=&to=&status=&limit=&offset=&property_id=
        Ritorna una lista (collection) di reservations.
        """
        self._ensure_login()
        params = {
            "from": date_from,
            "to": date_to,
            "limit": str(min(max(limit, 1), 200)),
            "offset": str(max(offset, 0)),
        }
        if status:
            params["status"] = status
        if property_id:
            params["property_id"] = str(property_id)

        url = f"{self.base_url}/api/public/reservations"
        r = self.session.get(url, headers=self._headers(), params=params, timeout=15)
        r.raise_for_status()
        js = r.json() or {}
        return ((js.get("data") or {}).get("collection") or [])

    def get_reservation_by_id(self, rid: str) -> Optional[Dict[str, Any]]:
        """
        GET /api/public/reservations/{id}
        """
        if not rid:
            return None
        self._ensure_login()
        url = f"{self.base_url}/api/public/reservations/{rid}"
        r = self.session.get(url, headers=self._headers(), timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        js = r.json() or {}
        return js.get("data") or None

    # --- Smart lookup
    def _pick_recent_confirmed_for_client(
        self,
        client_id: int,
        horizon_days_back: int = 60,
        horizon_days_forward: int = 60,
    ) -> Optional[Dict[str, Any]]:
        """
        Scarica finestre di reservation e sceglie quella più pertinente per il client_id:
        - status = confirmed
        - la più vicina ad oggi (oggi in soggiorno oppure futura breve)
        """
        today = date.today()
        from_str = (today - timedelta(days=horizon_days_back)).isoformat()
        to_str = (today + timedelta(days=horizon_days_forward)).isoformat()
        coll = self.list_reservations(from_str, to_str, status="confirmed", limit=200, offset=0)
        # Filtra client_id
        candidates = [r for r in coll if (r.get("client_id") == client_id)]
        if not candidates:
            return None

        # ordina per distanza dalla data di inizio rispetto ad oggi
        def score(res: Dict[str, Any]) -> Tuple[int, int]:
            s = (res.get("start_date") or "")[:10]
            try:
                sd = date.fromisoformat(s)
            except Exception:
                sd = today + timedelta(days=9999)
            return (abs((sd - today).days), -int(res.get("id") or 0))

        candidates.sort(key=score)
        chosen = candidates[0]

        # normalizza campi int->label
        chosen = dict(chosen)
        chosen["status"] = INT_STATUS.get(chosen.get("status"), chosen.get("status"))
        chosen["guest_status"] = INT_GUEST_STATUS.get(chosen.get("guest_status"), chosen.get("guest_status"))
        chosen["is_checkin_completed"] = INT_CHECKIN.get(chosen.get("is_checkin_completed"), chosen.get("is_checkin_completed"))

        return chosen

    def get_booking_context(self, phone: Optional[str] = None, reservation_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Ritorna:
        {
          "client": {...} | None,
          "reservation": {...} | None
        }
        Strategia:
          - Se reservation_id c'è → tenta get_reservation_by_id
          - Altrimenti, se phone c'è → search_clients(phone) → pick client → pick reservation recente confermata
        """
        self._ensure_login()
        out: Dict[str, Any] = {"client": None, "reservation": None}

        # 1) lookup diretto da reservation_id
        if reservation_id:
            try:
                res = self.get_reservation_by_id(reservation_id)
                if res:
                    # normalizza int
                    res = dict(res)
                    res["status"] = INT_STATUS.get(res.get("status"), res.get("status"))
                    res["guest_status"] = INT_GUEST_STATUS.get(res.get("guest_status"), res.get("guest_status"))
                    res["is_checkin_completed"] = INT_CHECKIN.get(res.get("is_checkin_completed"), res.get("is_checkin_completed"))
                    out["reservation"] = res
                    # client minimale se presente
                    if res.get("client"):
                        out["client"] = res["client"]
                # continua: se non trovata, proveremo via phone
            except Exception as e:
                logger.exception("get_booking_context/get_reservation_by_id error: %s", e)

        # 2) via phone → client → reservation
        if (not out.get("reservation")) and phone:
            try:
                col = self.search_clients(phone)
                client = (col[0] if col else None)
                if client:
                    out["client"] = client
                    cid = client.get("id")
                    if cid:
                        chosen = self._pick_recent_confirmed_for_client(cid)
                        if chosen:
                            out["reservation"] = chosen
            except Exception as e:
                logger.exception("get_booking_context via phone error: %s", e)

        return out
