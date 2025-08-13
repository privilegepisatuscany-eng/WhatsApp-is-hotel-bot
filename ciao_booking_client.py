import logging
from typing import Optional, Dict, Any
import requests

logger = logging.getLogger(__name__)

class CiaoBookingClient:
    def __init__(self, base_url: str, email: str, password: str, locale: str = "it"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.locale = locale
        self._token: Optional[str] = None
        self._token_exp: Optional[int] = None

    # ---------- auth ----------
    def _login(self) -> None:
        url = f"{self.base_url}/api/public/login"
        data = {"email": self.email, "password": self.password, "source": "pms"}
        headers = {"Accept-Language": self.locale}
        r = requests.post(url, data=data, headers=headers, timeout=15)
        r.raise_for_status()
        js = r.json().get("data") or {}
        self._token = js.get("token")
        self._token_exp = js.get("expiresAt")
        logger.info("CiaoBooking login OK; token valid until %s", self._token_exp)

    def _headers(self) -> Dict[str, str]:
        if not self._token:
            self._login()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ---------- endpoints ----------
    def get_client_by_phone(self, phone_normalized: str) -> Optional[Dict[str, Any]]:
        """Usa GET /api/public/clients/paginated?search=<phone>"""
        try:
            params = {
                "limit": "5",
                "page": "1",
                "search": phone_normalized,
                "order": "asc",
                "sortBy[]": "name",
            }
            url = f"{self.base_url}/api/public/clients/paginated"
            r = requests.get(url, headers=self._headers(), params=params, timeout=15)
            r.raise_for_status()
            coll = ((r.json() or {}).get("data") or {}).get("collection") or []
            if not coll:
                logger.info("CiaoBooking: client non trovato (%s)", phone_normalized)
                return None
            return coll[0]
        except requests.HTTPError as e:
            try:
                logger.error("CiaoBooking client error: %s", r.text)
            except Exception:
                pass
            logger.exception("Client lookup error: %s", e)
            return None
        except Exception as e:
            logger.exception("Client lookup exception: %s", e)
            return None

    def get_reservation_by_id(self, res_id: str) -> Optional[Dict[str, Any]]:
        """GET /api/public/reservations/{id}"""
        try:
            url = f"{self.base_url}/api/public/reservations/{res_id}"
            r = requests.get(url, headers=self._headers(), timeout=15)
            if r.status_code == 404:
                logger.error("CiaoBooking reservation 404: %s", res_id)
                return None
            r.raise_for_status()
            return (r.json() or {}).get("data") or None
        except requests.HTTPError as e:
            try:
                logger.error("CiaoBooking reservation error: %s", r.text)
            except Exception:
                pass
            logger.exception("Reservation error: %s", e)
            return None
        except Exception as e:
            logger.exception("Reservation exception: %s", e)
            return None

    def get_property(self, property_id: int) -> Optional[Dict[str, Any]]:
        """GET /api/public/property?id=<id> (ritorna collection con un item)"""
        try:
            params = {"id": str(property_id)}
            url = f"{self.base_url}/api/public/property"
            r = requests.get(url, headers=self._headers(), params=params, timeout=15)
            r.raise_for_status()
            coll = ((r.json() or {}).get("data") or {}).get("collection") or []
            return coll[0] if coll else None
        except Exception as e:
            logger.exception("Property lookup error: %s", e)
            return None
