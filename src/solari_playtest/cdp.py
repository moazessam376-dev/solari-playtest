"""A very small Chrome DevTools Protocol client over a websocket.

Enough for the lab: open a target, attach, navigate, evaluate JavaScript,
clear storage. No Playwright, no browser-use, so timings measure Solari and
the network rather than a client framework's bootstrap.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, url: str, *, timeout_s: float = 20.0) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._ws: Any = None
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._listeners: list = []

    async def __aenter__(self) -> CDP:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._ws = await asyncio.wait_for(
            websockets.connect(self.url, max_size=64 * 1024 * 1024, open_timeout=self.timeout_s),
            self.timeout_s,
        )
        self._reader = asyncio.create_task(self._read_loop())

    def on_event(self, cb) -> None:
        """Subscribe to every CDP event message (those without an id)."""
        self._listeners.append(cb)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif "method" in msg:
                    for cb in self._listeners:
                        try:
                            cb(msg)
                        except Exception:  # noqa: BLE001 - a listener must never kill the reader
                            pass
        except Exception as err:  # noqa: BLE001 - connection gone; fail waiters
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(f"CDP connection closed: {err}"))
            self._pending.clear()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("not connected")
        self._next += 1
        mid = self._next
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(msg))
        res = await asyncio.wait_for(fut, self.timeout_s)
        if "error" in res:
            raise CDPError(f"{method}: {res['error'].get('message')}")
        return res.get("result", {})

    # --- conveniences ---------------------------------------------------------

    async def new_page(self, url: str = "about:blank") -> str:
        """Create a page target and attach to it; returns the session id."""
        target = await self.send("Target.createTarget", {"url": url})
        att = await self.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        return att["sessionId"]

    async def navigate(self, session: str, url: str, settle_s: float = 1.5) -> None:
        await self.send("Page.enable", session_id=session)
        await self.send("Page.navigate", {"url": url}, session_id=session)
        await asyncio.sleep(settle_s)

    async def evaluate(self, session: str, expression: str) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session,
        )
        return res.get("result", {}).get("value")

    async def wipe(self, session: str) -> None:
        await self.send("Network.clearBrowserCookies", session_id=session)
        await self.send(
            "Storage.clearDataForOrigin", {"origin": "*", "storageTypes": "all"}, session_id=session
        )
