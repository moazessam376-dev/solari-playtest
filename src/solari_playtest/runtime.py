"""Where the game runs: a Solari sandbox that builds and serves it, and a Solari
browser that plays it. Everything here is game-agnostic.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .cdp import CDP
from .client import SolariClient, SolariError

GIT_URL = re.compile(r"^(https?://|git@)[^\s]+?(\.git)?/?$")


def looks_like_git(target: str) -> bool:
    return "github.com" in target or "gitlab.com" in target or target.endswith(".git")


# --- sandbox: build from git and serve ----------------------------------------


@dataclass
class Served:
    sandbox_id: str
    url: str
    commit: str
    build_log: str
    build_s: float


class SandboxBuilder:
    """`POST /sandboxes` + `POST /sandboxes/:id/exec` + `GET /sandboxes/:id/ports/:port`."""

    def __init__(self, client: SolariClient) -> None:
        self.c = client
        self.sandbox_id: str | None = None

    async def _exec(
        self, cmd: str, args: list[str], cwd: str | None = None, timeout_ms: int = 300_000
    ) -> dict[str, Any]:
        res = await self.c.request(
            "POST",
            f"/sandboxes/{self.sandbox_id}/exec",
            {"cmd": cmd, "args": args, "cwd": cwd, "timeoutMs": timeout_ms},
        )
        if res.status_code >= 400:
            raise SolariError(f"exec failed: {res.status_code} {res.text[:200]}", res.status_code)
        return res.json()

    async def sh(self, script: str, timeout_ms: int = 300_000) -> dict[str, Any]:
        return await self._exec("sh", ["-c", script], timeout_ms=timeout_ms)

    async def serve_from_git(
        self,
        repo: str,
        ref: str | None = None,
        *,
        build_cmd: str = "npm install --no-audit --no-fund && npx vite build",
        serve_dir: str = "dist",
        port: int = 4173,
        cpu: int = 2,
        mem_mb: int = 4096,
        timeout_min: int = 30,
    ) -> Served:
        t0 = time.perf_counter()
        res = await self.c.request(
            "POST",
            "/sandboxes",
            {
                "template": "base",
                "kind": "sandbox",
                "cpu": cpu,
                "memMb": mem_mb,
                "timeoutMs": timeout_min * 60_000,
            },
        )
        if res.status_code >= 400:
            raise SolariError(f"POST /sandboxes failed: {res.status_code} {res.text[:200]}", res.status_code)
        self.sandbox_id = res.json()["sandboxId"]
        log: list[str] = []
        clone_url = repo
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token and repo.startswith("https://github.com/"):
            clone_url = repo.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
        branch = f"--branch {ref} " if ref and not re.fullmatch(r"[0-9a-f]{7,40}", ref) else ""
        clone = (
            f"git clone --depth 1 {branch}{clone_url} /work/game 2>&1 | sed 's#x-access-token:[^@]*@#***@#'"
        )
        r = await self.sh(clone, 600_000)
        log.append(r.get("stdout", "") + r.get("stderr", ""))
        if r.get("exitCode"):
            raise SolariError(f"git clone failed: {log[-1][-300:]}")
        if ref and re.fullmatch(r"[0-9a-f]{7,40}", ref):
            r = await self.sh(f"cd /work/game && git fetch --depth 1 origin {ref} && git checkout {ref} 2>&1")
            log.append(r.get("stdout", "") + r.get("stderr", ""))
        commit = (await self.sh("cd /work/game && git rev-parse HEAD")).get("stdout", "").strip()
        r = await self.sh(f"cd /work/game && ({build_cmd}) 2>&1 | tail -40", 900_000)
        log.append(r.get("stdout", "") + r.get("stderr", ""))
        if r.get("exitCode"):
            raise SolariError(f"build failed (exit {r.get('exitCode')}): {log[-1][-400:]}")
        await self.sh(
            f"cd /work/game/{serve_dir} && nohup python3 -m http.server {port} >/tmp/http.log 2>&1 & sleep 1"
        )
        res = await self.c.request("GET", f"/sandboxes/{self.sandbox_id}/ports/{port}")
        if res.status_code >= 400:
            raise SolariError(f"preview url failed: {res.status_code} {res.text[:200]}", res.status_code)
        url = res.json()["url"]
        async with httpx.AsyncClient(timeout=30) as pub:
            for _ in range(15):
                try:
                    if (await pub.get(url)).status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        return Served(self.sandbox_id, url, commit, "\n".join(log), time.perf_counter() - t0)

    async def kill(self) -> None:
        if self.sandbox_id:
            try:
                await self.c.request("DELETE", f"/sandboxes/{self.sandbox_id}")
            finally:
                self.sandbox_id = None


# --- browser: open and observe ----------------------------------------------------


@dataclass
class ConsoleEntry:
    level: str
    text: str
    t: float


@dataclass
class Game:
    """A live Solari browser on the game, with CDP session and observers."""

    session_id: str
    cdp: CDP
    page: str
    url: str
    width: int
    height: int
    console: list[ConsoleEntry] = field(default_factory=list)
    run_dir: Path = field(default_factory=lambda: Path("runs") / time.strftime("%Y%m%d-%H%M%S"))
    shots: int = 0

    async def evaluate(self, js: str) -> Any:
        return await self.cdp.evaluate(self.page, js)

    async def screenshot(self, label: str = "shot", clip: dict[str, Any] | None = None) -> Path:
        params: dict[str, Any] = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        res = await self.cdp.send("Page.captureScreenshot", params, session_id=self.page)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shots += 1
        path = self.run_dir / f"{self.shots:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}.png"
        path.write_bytes(base64.b64decode(res["data"]))
        return path

    async def canvas_variance(self) -> dict[str, Any]:
        """How many distinct colours the first canvas paints; 1 means a flat layer."""
        rect = await self.evaluate(
            "(() => { const c=document.querySelector('canvas'); if(!c) return null; const r=c.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })()"
        )
        if not rect or rect["width"] < 2:
            return {"canvas": False}
        res = await self.cdp.send(
            "Page.captureScreenshot", {"format": "png", "clip": {**rect, "scale": 1}}, session_id=self.page
        )
        im = Image.open(io.BytesIO(base64.b64decode(res["data"]))).convert("RGB").resize((160, 90))
        colors = len(set(im.getdata()))
        return {"canvas": True, "distinct_colours": colors, "rect": rect}

    async def webgl(self) -> dict[str, Any]:
        return await self.evaluate(
            "(() => { const c=document.createElement('canvas'); const gl=c.getContext('webgl2')||c.getContext('webgl'); if(!gl) return {webgl:false};"
            " const d=gl.getExtension('WEBGL_debug_renderer_info'); return {webgl:true, version:gl.getParameter(gl.VERSION),"
            " renderer: d?gl.getParameter(d.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER)}; })()"
        )

    async def fps(self, seconds: float = 1.0) -> float:
        n = await self.evaluate(
            f"new Promise(r=>{{let n=0;const t0=performance.now();function f(){{n++; if(performance.now()-t0<{seconds * 1000}) requestAnimationFrame(f); else r(n);}} requestAnimationFrame(f);}})"
        )
        return float(n or 0) / seconds

    def new_console_errors(self, since: float) -> list[ConsoleEntry]:
        return [c for c in self.console if c.t >= since and c.level in ("error", "exception")]


class Browser:
    """Opens a Solari browser session on a URL and wires the observers."""

    def __init__(self, client: SolariClient) -> None:
        self.c = client
        self.session_id: str | None = None
        self._cdp: CDP | None = None

    async def open(
        self,
        url: str,
        *,
        width: int = 1280,
        height: int = 720,
        recording: bool = True,
        run_dir: Path | None = None,
    ) -> Game:
        s = await self.c.create_session(recording=recording)
        self.session_id = s.id
        cdp = CDP(s.cdp_endpoint, timeout_s=30)
        await cdp.connect()
        self._cdp = cdp
        page = await cdp.new_page()
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            session_id=page,
        )
        await cdp.send("Page.enable", session_id=page)
        await cdp.send("Runtime.enable", session_id=page)
        await cdp.send("Log.enable", session_id=page)
        game = Game(
            s.id,
            cdp,
            page,
            url,
            width,
            height,
            run_dir=run_dir or Path("runs") / time.strftime("%Y%m%d-%H%M%S"),
        )
        cdp.on_event(lambda m: self._observe(game, m))
        await cdp.send("Page.navigate", {"url": url}, session_id=page)
        for _ in range(60):
            if await cdp.evaluate(page, "document.readyState") == "complete":
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.0)
        return game

    @staticmethod
    def _observe(game: Game, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        p = msg.get("params", {})
        if method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning", "assert"):
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            game.console.append(
                ConsoleEntry("error" if p["type"] != "warning" else "warning", text[:500], time.time())
            )
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            text = d.get("exception", {}).get("description") or d.get("text", "")
            game.console.append(ConsoleEntry("exception", str(text)[:500], time.time()))
        elif method == "Log.entryAdded" and p.get("entry", {}).get("level") in ("error",):
            e = p["entry"]
            game.console.append(
                ConsoleEntry("error", f"{e.get('source')}: {e.get('text', '')}"[:500], time.time())
            )

    async def close(self) -> str | None:
        if self._cdp:
            await self._cdp.close()
            self._cdp = None
        sid = self.session_id
        if sid:
            try:
                await self.c.release_session(sid)
            except SolariError:
                pass
            self.session_id = None
        return sid

    async def replay_url(self, session_id: str) -> str | None:
        try:
            return (await self.c.wait_for_replay_url(session_id, timeout_s=45))["url"]
        except SolariError:
            return None


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
