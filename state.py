
import time
from typing import Dict, Any

_TTL_SECONDS = 60 * 30  # 30 minuti

class _StateStore:
    def __init__(self):
        self._data: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Dict[str, Any]:
        s = self._data.get(key, {})
        if not s:
            return {}
        if s.get("_expires_at", 0) < time.time():
            self._data.pop(key, None)
            return {}
        return s

    def set(self, key: str, value: Dict[str, Any], ttl: int = _TTL_SECONDS):
        value = dict(value)
        value["_expires_at"] = time.time() + ttl
        self._data[key] = value

    def reset(self, key: str):
        self._data.pop(key, None)

STATE = _StateStore()
