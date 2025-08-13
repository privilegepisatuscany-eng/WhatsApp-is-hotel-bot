# ciao_booking_client.py
import os
import time
import logging
from typing import Any, Dict, Optional

from datetime import date, timedelta
import requests

log = logging.getLogger("ciaobooking")

class CiaoBookingError(Exception):
    pass

class CiaoBookingClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        locale: str = "it",
        timeout: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale or "it"
        self.timeout = timeout

        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._sess = requests.Session()
        self._sess.headers.update({
            "Accept": "application/json",
            "User-Agent": "whatsapp-is-hotel-bot/1.0",
            "Accept-Language": self.locale,
        })

    # ---------- Low level ----------
    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _login(self) -> None:
        url = f"{self.base_url}/api/public/login"
        payload = {"email": self.email, "password": self.password}
        r = self._sess.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json() if r.content else {}
        node = data.get("data", data)
        self._token = node.get("token")
        expires_at = node.get("expires_at")
        if isinstance(expires_at, (int, float)):
            self._token_exp = float(expires_at)
        else:
            self._token_exp = time.time() + 50 * 60
        log.info("CiaoBooking login OK; token valid until %s", int(self._token_exp))

    def _ensure_token(self) -> None:
        if not self._token or time.time() >= (self._token_exp - 60):
            self._login()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self._ensure_token()
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        headers.setdefault("Accept-Language", self.locale)
        r = self._sess.request(method=method.upper(), url=url, headers=headers, timeout=self.timeout, **kwargs)
        if r.status_code == 401:
            log.warning("401 su %s, ritento login…", path)
            self._login()
            headers.update(self._auth_headers())
            r = self._sess.request(method=method.upper(), url=url, headers=headers, timeout=self.timeout, **kwargs)
        r.raise_for_status()
        return r

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = self._request("GET", path, params=params or {})
        return r.json() if r.content else {}

    # ---------- Resources ----------
    def search_clients_by_phone(self, phone: str) -> Dict[str, Any]:
        params = {
            "limit": 5,
            "page": 1,
            "search": phone,
            "order": "asc",
            "sortBy[]": "name",
        }
        data = self._get("/api/public/clients/paginated", params=params)
        return data.get("data", data)

    def get_reservation(self, reservation_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = self._get(f"/api/public/reservations/{reservation_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        node = data.get("data", data)
        return node or None

    def list_reservations(
        self,
        from_date: str,
        to_date: str,
        status: Optional[str] = None,
        property_id: Optional[int] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params = {
            "from": from_date,
            "to": to_date,
            "limit": str(limit),
            "offset": str(offset),
        }
        if status:
            params["status"] = status  # es. "confirmed"
        if property_id:
            params["property_id"] = str(property_id)
        data = self._get("/api/public/reservations", params=params)
        return data.get("data", data)

def find_recent_reservation_for_client(
    self,
    client_id: int,
    days_back: int = 365,
    days_forward: int = 365,
) -> Optional[Dict[str, Any]]:
    """
    Cerca una reservation per client_id in una finestra ampia.
    Ordina per rilevanza: in corso, future, passate, priorità status, distanza.
    """
    from datetime import date, timedelta

    today = date.today()
    frm = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to  = (today + timedelta(days=days_forward)).strftime("%Y-%m-%d")

    raw = self.list_reservations(from_date=frm, to_date=to, status=None, limit=200, offset=0)
    coll = (raw.get("collection") if isinstance(raw, dict) else None) or []

    candidates = []
    for r in coll:
        cid = r.get("client_id") or (r.get("client") or {}).get("id")
        if cid == client_id:
            candidates.append(r)

    if not candidates:
        return None

    status_rank = {"CONFIRMED": 0, "PENDING": 1}
    def rank(res):
        try:
            ds = date.fromisoformat(res.get("start_date", "")[:10])
            de = date.fromisoformat(res.get("end_date", "")[:10])
        except Exception:
            ds, de = date.min, date.max

        if ds <= today <= de:
            pos = 0
        elif ds > today:
            pos = 1
        else:
            pos = 2

        st_norm = (res.get("status") or "").upper()
        s_rank = status_rank.get(st_norm, 9)
        dist = abs((ds - today).days)

        return (pos, s_rank, dist)

    candidates.sort(key=rank)
    return candidates[0]

    # ---------- High level ----------
    def get_booking_context(
        self,
        phone: Optional[str] = None,
        reservation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ritorna:
        {
          "client": {...} | None,
          "reservation": {...} | None
        }
        """
        ctx: Dict[str, Any] = {"client": None, "reservation": None}

        # (1) Reservation esplicita
        if reservation_id:
            res = self.get_reservation(reservation_id)
            if res:
                ctx["reservation"] = self._normalize_reservation(res)
                if "property_name" not in ctx["reservation"]:
                    prop = (res.get("property") or {}).get("name")
                    if prop:
                        ctx["reservation"]["property_name"] = prop

        # (2) Client da telefono
        client = None
        if phone:
            try:
                raw = self.search_clients_by_phone(phone)
                coll = (raw.get("collection") if isinstance(raw, dict) else None) or []
                client = coll[0] if coll else None
                if client:
                    ctx["client"] = client
            except requests.HTTPError as e:
                log.error("Errore search_clients_by_phone: %s", e)

        # (3) Se ho client ma non ho reservation, cerco reservation recente CONFIRMED
        if (not ctx.get("reservation")) and client and client.get("id"):
            try:
                found = self.find_recent_reservation_for_client(client_id=client["id"], days_back=30, days_forward=30)
                if found:
                    ctx["reservation"] = self._normalize_reservation(found)
                    if "property_name" not in ctx["reservation"]:
                        prop = (found.get("property") or {}).get("name")
                        if prop:
                            ctx["reservation"]["property_name"] = prop
                    log.info("Reservation agganciata per client_id=%s: id=%s",
                             client["id"], ctx["reservation"].get("id") or ctx["reservation"].get("res_id"))
                else:
                    log.info("Nessuna reservation recente per client_id=%s", client["id"])
            except Exception as e:
                log.error("Errore find_recent_reservation_for_client: %s", e)

        return ctx

    # ---------- Helpers ----------
    @staticmethod
    def _normalize_reservation(res: Dict[str, Any]) -> Dict[str, Any]:
        status_map = {1: "CANCELED", 2: "CONFIRMED", 3: "PENDING"}
        guest_map = {0: "NOT_ARRIVED", 1: "ARRIVED", 2: "LEFT"}
        checkin_map = {0: "TO_DO", 1: "COMPLETED", 2: "VERIFIED"}

        def _map(v, m):
            if isinstance(v, str):
                return v
            return m.get(v, v)

        out = dict(res)
        out["status"] = _map(res.get("status"), status_map)
        out["guest_status"] = _map(res.get("guest_status"), guest_map)
        out["is_checkin_completed"] = _map(res.get("is_checkin_completed"), checkin_map)
        if "property_name" not in out:
            name = (res.get("property") or {}).get("name")
            if name:
                out["property_name"] = name
        return out
