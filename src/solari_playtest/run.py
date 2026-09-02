"""One playtest run, start to finish: serve, open, smoke, discover, stress, report."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .client import SolariClient
from .explore import Explorer
from .report import write_html, write_json, write_markdown
from .runtime import Browser, SandboxBuilder, Served, SessionLost, looks_like_git
from .tools import Tools


def load_hints(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    return yaml.safe_load(p.read_text()) or {} if p.exists() else {}


async def playtest(
    target: str,
    *,
    ref: str | None = None,
    hints: dict[str, Any] | None = None,
    viewport: tuple[int, int] = (1280, 720),
    out: Path | None = None,
    depth: int = 3,
    max_sequences: int = 80,
    build_cmd: str | None = None,
    serve_dir: str = "dist",
    keep_sandbox: bool = False,
    agent: bool = False,
    agent_steps: int = 60,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    progress = progress or (lambda *_: None)
    hints = hints or {}
    out = out or Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    client = SolariClient()
    builder: SandboxBuilder | None = None
    served: Served | None = None
    url = target
    try:
        if looks_like_git(target):
            progress("build", f"building {target} in a Solari sandbox")
            builder = SandboxBuilder(client)
            kw: dict[str, Any] = {"serve_dir": hints.get("serve_dir", serve_dir)}
            if build_cmd or hints.get("build_cmd"):
                kw["build_cmd"] = build_cmd or hints["build_cmd"]
            served = await builder.serve_from_git(target, ref, **kw)
            url = served.url
            progress("build", f"served at commit {served.commit[:10]} in {served.build_s:.0f}s")
        browser = Browser(client)
        progress("open", f"opening {url.split('?')[0]}")
        game = await browser.open(url, width=viewport[0], height=viewport[1], run_dir=out)
        if builder is not None:
            game.refresh_url = lambda: builder.preview_url(builder.port)
        tools = Tools(game)
        ex = Explorer(tools, hints=hints, on_progress=progress)

        async def fresh() -> None:
            try:
                await game.send("Page.navigate", {"url": await game.current_url()})
            except SessionLost:
                pass  # a new session is already open on the game URL
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await game.evaluate("document.readyState") == "complete":
                    break
            await asyncio.sleep(0.8)
            await ex.start()

        ex.fresh = fresh
        meta: dict[str, Any] = {
            "target": target.split("?")[0],
            "url": url.split("?")[0],
            "commit": served.commit if served else None,
            "viewport": f"{viewport[0]}x{viewport[1]}",
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "browser_session": game.session_id,
            "sandbox": served.sandbox_id if served else None,
        }
        try:
            await tools.screenshot("load")
            started = await ex.start()
            progress("start", str(started))
            smoke = await ex.smoke()
            meta.update(
                {
                    "webgl": smoke["webgl"],
                    "canvas": smoke["canvas"],
                    "fps": smoke["fps"],
                    "page_title": smoke.get("title"),
                }
            )
            await tools.screenshot("after start")
            controls = []
            try:
                progress("discover", "probing keys and buttons")
                controls = await ex.discover(keys=hints.get("keys"), buttons=hints.get("buttons", True))
                progress("discover", f"{len(controls)} controls: " + ", ".join(c.label() for c in controls))
                progress("stress", "running open/close sequences")
                await ex.ui_stress(depth=depth, max_sequences=max_sequences)
            except Exception as err:  # noqa: BLE001 - keep what was found
                meta["aborted"] = f"{type(err).__name__}: {str(err)[:200]}"
                progress("stress", f"aborted: {meta['aborted']}")
            sequences = min(max_sequences, _count_sequences(len([c for c in controls if c.opens]), depth))
            meta["cdp_recoveries"] = game.recoveries
            meta["session_restarts"] = game.restarts
        finally:
            sid = await browser.close()
            meta["replay_url"] = await browser.replay_url(sid) if sid else None
        agent_run: dict[str, Any] | None = None
        if agent:
            from .agent import run_agent

            progress("agent", "autonomous browser-use pass")
            agent_run = await run_agent(url, hints=hints, out=out, max_steps=agent_steps, viewport=viewport)
            progress("agent", f"{len(agent_run['findings'])} finding(s) in {agent_run['meta']['seconds']}s")
        run = {
            "meta": meta,
            "controls": [{"label": c.label(), **c.__dict__} for c in ex.controls],
            "sequences": sequences,
            "findings": [f.as_dict() for f in ex.findings] + (agent_run["findings"] if agent_run else []),
            "actions": tools.log.steps,
            "agent": agent_run,
        }
        write_json(out / "report.json", run)
        write_markdown(out / "report.md", run)
        write_html(out / "report.html", run)
        run["out"] = str(out)
        return run
    finally:
        if builder and not keep_sandbox:
            await builder.kill()
        await client.close()


def _count_sequences(n: int, depth: int) -> int:
    pairs = n * (n - 1)
    return n + pairs * 3 + (n * (n - 1) * (n - 2) if depth >= 3 else 0)
