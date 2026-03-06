"""Proxy utilities for loading and rotating proxy endpoints."""

from __future__ import annotations

import random
from pathlib import Path
from typing import List, Optional


class ProxyManager:
    """Loads proxy endpoints from a file and returns one on demand."""

    def __init__(self, proxy_file: str) -> None:
        self.proxy_file = Path(proxy_file).expanduser()
        self._proxies: List[str] = []
        self.reload()

    def reload(self) -> None:
        """Reload proxy endpoints from disk."""
        self._proxies = []
        if not self.proxy_file.exists():
            return
        for raw in self.proxy_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self._proxies.append(line)

    def get_proxy(self) -> Optional[dict]:
        """Return a random proxy as a Playwright/Selenium compatible dict."""
        if not self._proxies:
            return None
        return {"server": random.choice(self._proxies)}

    @property
    def size(self) -> int:
        """Return number of loaded proxies."""
        return len(self._proxies)
