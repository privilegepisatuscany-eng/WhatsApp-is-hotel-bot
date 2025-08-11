import time
import threading

class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def clear(self, key):
        with self._lock:
            if key in self._data:
                del self._data[key]
