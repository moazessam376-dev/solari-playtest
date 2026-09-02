"""Async client for the parts of the Solari HTTP API that solab needs.

Browser sessions: create, release, replay. Profiles: list, create, delete.
Sandboxes, desktops, templates, snapshots, volumes: list and delete. Plus a
raw CDP connect used by `bench` so no Playwright or browser-use is required.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.getsolari.com"
REGION_URLS: dict[str, str] = {"us-west": DEFAULT_BASE_URL}
ProxySpec = str | Mapping[str, Any]


class SolariError(RuntimeError):
    def __init__(
        self, message: str, status: int | None = None, code: str | None = None, body: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


def _parse_body(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _code(body: Any) -> str | None:
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return body["code"]
    return None


@dataclass
class Session:
    id: str
    ws_endpoint: str
    cdp_endpoint: str
    expires_at: str | None
    proxy: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return self.id.split(":", 1)[0]


def derive_cdp_from_ws(ws_endpoint: str) -> str:
    idx = ws_endpoint.find("/ws/")
    return ws_endpoint if idx == -1 else ws_endpoint[:idx] + "/cdp/" + ws_endpoint[idx + 4 :]


class SolariClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        region: str = "us-west",
        base_url: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        api_key = api_key or os.environ.get("SOLARI_API_KEY")
        if not api_key:
            raise SolariError("Solari API key missing: pass --api-key or set SOLARI_API_KEY")
        if base_url is None:
            if region not in REGION_URLS:
                raise SolariError(f"unsupported region {region!r}; known: {', '.join(REGION_URLS)}")
            base_url = REGION_URLS[region]
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SolariClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout_s,
                headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "solari-playtest/0.1"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(self, method: str, path: str, body: Any | None = None) -> httpx.Response:
        http = await self._client()
        return (
            await http.request(method, path, json=body)
            if body is not None
            else await http.request(method, path)
        )

    async def _json(self, method: str, path: str, body: Any | None = None, *, ok404: bool = False) -> Any:
        res = await self.request(method, path, body)
        parsed = _parse_body(res.text)
        if res.status_code >= 400 and not (ok404 and res.status_code == 404):
            raise SolariError(
                f"{method} {path} failed: {res.status_code} {res.text[:300]}",
                res.status_code,
                _code(parsed),
                parsed,
            )
        return parsed

    # --- browser sessions ---------------------------------------------------

    async def create_session(
        self,
        *,
        profile_id: str | None = None,
        recording: bool = False,
        stealth: bool = False,
        captcha: bool = False,
        web_bot_auth: bool = False,
        proxy: ProxySpec | None = None,
    ) -> Session:
        body: dict[str, Any] = {}
        if profile_id:
            body["profileId"] = profile_id
        for k, v in (
            ("recording", recording),
            ("stealth", stealth),
            ("captcha", captcha),
            ("webBotAuth", web_bot_auth),
        ):
            if v:
                body[k] = True
        if proxy is not None:
            body["proxy"] = dict(proxy) if isinstance(proxy, Mapping) else proxy
        data = await self._json("POST", "/sessions", body or None)
        if not isinstance(data, dict) or not data.get("sessionId") or not data.get("wsEndpoint"):
            raise SolariError(f"unexpected /sessions response: {json.dumps(data)[:300]}")
        return Session(
            id=data["sessionId"],
            ws_endpoint=data["wsEndpoint"],
            cdp_endpoint=data.get("cdpEndpoint") or derive_cdp_from_ws(data["wsEndpoint"]),
            expires_at=data.get("expiresAt"),
            proxy=data.get("proxy"),
            raw=data,
        )

    async def release_session(self, session_id: str) -> bool:
        """True if the gateway confirmed the release, False if it was already gone."""
        res = await self.request("DELETE", f"/sessions/{session_id}")
        parsed = _parse_body(res.text)
        if res.status_code < 400:
            return True
        if res.status_code == 404 and _code(parsed) != "InvalidSessionId":
            return False
        raise SolariError(
            f"DELETE /sessions/{session_id} failed: {res.status_code} {res.text[:200]}",
            res.status_code,
            _code(parsed),
            parsed,
        )

    async def replay_url(self, session_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{session_id}/replay-url")

    async def wait_for_replay_url(
        self, session_id: str, *, timeout_s: float = 45.0, interval_s: float = 1.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return await self.replay_url(session_id)
            except SolariError as err:
                if err.status not in (404, 409, 425) or time.monotonic() >= deadline:
                    raise
            await asyncio.sleep(interval_s)

    async def download_replay(self, session_id: str) -> bytes:
        url = (await self.wait_for_replay_url(session_id))["url"]
        async with httpx.AsyncClient(timeout=60) as h:
            r = await h.get(url)
            r.raise_for_status()
            return r.content

    # --- profiles -------------------------------------------------------------

    async def list_profiles(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/profiles")
        return data if isinstance(data, list) else (data or {}).get("profiles", [])

    async def create_profile(self, name: str) -> dict[str, Any]:
        return await self._json("POST", "/profiles", {"name": name})

    async def delete_profile(self, profile_id: str) -> None:
        await self._json("DELETE", f"/profiles/{profile_id}", ok404=True)

    # --- VMs ------------------------------------------------------------------

    async def list_sandboxes(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/sandboxes")
        return (data or {}).get("sandboxes", []) if isinstance(data, dict) else (data or [])

    async def list_templates(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/templates")
        return (data or {}).get("templates", []) if isinstance(data, dict) else (data or [])

    async def list_snapshots(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/snapshots")
        return (data or {}).get("snapshots", []) if isinstance(data, dict) else (data or [])

    async def list_volumes(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/volumes")
        return (data or {}).get("volumes", []) if isinstance(data, dict) else (data or [])

    async def kill_sandbox(self, sandbox_id: str) -> None:
        await self._json("DELETE", f"/sandboxes/{sandbox_id}", ok404=True)
