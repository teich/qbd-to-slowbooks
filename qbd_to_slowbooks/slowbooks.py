"""A small client for the SlowBooks Pro REST API (``/openapi.json`` on any
server documents every route)."""

from __future__ import annotations

import time

import httpx


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {path} -> {status}: {detail}")


class SlowBooks:
    def __init__(self, base_url: str, token: str, timeout: float = 120):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def request(self, method: str, path: str, *, retries: int = 3, **kwargs):
        for attempt in range(retries + 1):
            try:
                resp = self.http.request(method, path, **kwargs)
            except httpx.ConnectError:
                if attempt == retries:
                    raise
            else:
                # Retry only what never reached the app: a POST that timed
                # out may have posted, and a second copy would unbalance
                # nothing but still double the history.
                if resp.status_code not in (502, 503) or attempt == retries:
                    break
            time.sleep(2**attempt)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ApiError(method, path, resp.status_code, detail)
        return resp.json() if resp.content else None

    def get(self, path: str, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict | None = None):
        return self.request("POST", path, json=body or {})

    def put(self, path: str, body: dict):
        return self.request("PUT", path, json=body)
