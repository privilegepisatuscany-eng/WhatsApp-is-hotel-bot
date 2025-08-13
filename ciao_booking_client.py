# ciao_booking_client.py
import time
import logging
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("ciaobooking")


class CiaoBookingError(Exception):
    pass


class CiaoBookingClient:
    """
    Client minimale e robusto per le API CiaoBooking lato bot.
    - Login con bearer token e auto-refresh
    - Ricerca client per telefono
    - Get reservation by ID (se presente)
    - Fallback: cerca tra le prenotazioni confermate in un range di date e
      sceglie la più recente del client trovato
    - Consegna un 'booking_ctx' coerente per il bot:
        {
          "client": {...} | None,
          "reservation": {...} | None
        }
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        locale: str = "it",
        timeout: int = 10,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale or "it"
        self.timeout = timeout

        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._sess = requests.Session()
        self._sess.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "whatsapp-is-hotel-bot/1.0",
                "Accept-Language": self.locale,
            }
        )

    # ---------- Low level ----------
    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _login(self) -> None:
        url = f"{self.base_url}/api/public/login"
        payload = {"email": self.email, "password": self.password}
        r = self._sess.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data: Any = r.json() if r.content else {}
        node = data.get("data", data) if isinstance(data, dict) else {}
        self._token = node.get("token")
        # scadenza: usa expires_at se fornito, altrimenti 50 minuti
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
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())
        headers.setdefault("Accept-Language", self.locale)
        r = self._sess.request(
            method=method.upper(),
            url=url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        if r.status_code == 401:
            # token scaduto/non valido: riloggiamo una volta
            log.warning("401 su %s, ritento login…", path)
            self._login()
            headers.update(self._auth_headers())
            r = self._sess.request(
                method=method.upper(),
                url=url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        r.raise_for_status()
        return r

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._request("GET", path, params=params or {})
        if not r.content:
            return {}
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            try:
                return r.json()
            except Exception:
                # se il server risponde JSON invalido, proteggiamo il chiamante
                return {}
        # come fallback, restituiamo raw text (anche se non dovrebbe servire)
        return r.text

    # ---------- Resources ----------
    def search_clients_by_phone(self, phone: str) -> Dict[str, Any]:
        """
        Cerca i client filtrando per 'search' (nome/email/telefono).
        Ritorna sempre un dict (compat):
            { "collection": [...], ... } se possibile
        """
        params = {
            "limit": 5,
            "page": 1,
            "search": phone,
            "order": "asc",
            "sortBy[]": "name",
        }
        raw = self._get("/api/public/clients/paginated", params=params)

        if isinstance(raw, dict):
            return raw.get("data", raw)  # compat
        # fallback: incapsula in un dict coerente
        coll = raw if isinstance(raw, list) else []
        return {"collection": coll}

    def get_reservation(self, reservation_id: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._get(f"/api/public/reservations/{reservation_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

        node = raw.get("data", raw) if isinstance(raw, dict) else None
        return node or None

    # ---------- High level used by app.py ----------
    def get_booking_context(
        self,
        phone: Optional[str] = None,
        reservation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Restituisce un contesto coerente per il bot:
        {
          "client": {...} | None,
          "reservation": {...} | None
        }
        """
        ctx: Dict[str, Any] = {"client": None, "reservation": None}

        # 1) Se ho un reservation_id, priorità alta
        if reservation_id:
            res = self.get_reservation(reservation_id)
            if res:
                ctx["reservation"] = self._normalize_reservation(res)
                # include property_name se possibile
                if "property_name" not in ctx["reservation"]:
                    prop = (res.get("property") or {}).get("name")
                    if prop:
                        ctx["reservation"]["property_name"] = prop

        # 2) Se ho un telefono, provo a cercare il client
        client: Optional[Dict[str, Any]] = None
        if phone:
            try:
                raw = self.search_clients_by_phone(phone)
                coll = (
                    raw.get("collection") if isinstance(raw, dict) else None
                ) or []
                client = coll[0] if coll else None
                if client:
                    ctx["client"] = client
            except requests.HTTPError as e:
                log.error("Errore search_clients_by_phone: %s", e)
                # Non rilancio: il bot deve continuare

        # 3) Se non ho ancora una reservation ma ho il client,
        #    provo a cercare tra le confermate in un range sensato (±60 gg)
        if not ctx.get("reservation") and client:
            from datetime import date, timedelta

            start = date.today() - timedelta(days=60)
            end = date.today() + timedelta(days=60)
            try:
                data = self._get(
                    "/api/public/reservations",
                    params={
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "limit": 200,
                        "offset": 0,
                        "status": "confirmed",
                    },
                )

                # --- ROBUSTEZZA TIPI ---
                if isinstance(data, dict):
                    rows = data.get("data") or data.get("collection") or []
                elif isinstance(data, list):
                    rows = data
                else:
                    rows = []

                client_id = client.get("id")
                picked = None
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    c = r.get("client") or {}
                    if c.get("id") == client_id:
                        picked = r
                        # (se servisse ordinare per data, qui possiamo confrontare; per ora prima match)
                        break

                if picked:
                    ctx["reservation"] = self._normalize_reservation(picked)
                else:
                    log.info("Nessuna reservation recente per client_id=%s", client_id)
            except requests.HTTPError as e:
                log.error("Errore list_reservations_confirmed_between: %s", e)
            except Exception as e:
                # Protegge da casi strani: es. server che ritorna stringhe
                log.error("Errore lookup CiaoBooking (fallback reservations): %s", e)

        return ctx

    # ---------- Helpers ----------
    @staticmethod
    def _normalize_reservation(res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converte i codici numerici in label testuali (quando arrivano numeri).
        Accetta anche payload già ‘stringificati’.
        """
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

        # scorciatoie utili all'app
        if "property_name" not in out:
            name = (res.get("property") or {}).get("name")
            if name:
                out["property_name"] = name
        return out
