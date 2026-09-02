"""`playtest-mcp`: the playtest primitives as MCP tools, so Claude Code, Codex
or Cursor can drive a Solari browser on your game and read the findings.

Register in `.mcp.json`:

    {"mcpServers": {"playtest": {"command": "playtest-mcp", "env": {"SOLARI_API_KEY": "slr_live_..."}}}}

One game is open at a time. `playtest_open` builds or points at it,
`playtest_run` does the whole deterministic pass in one call.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .client import SolariClient
from .explore import Explorer
from .report import write_html, write_json, write_markdown
from .run import playtest
from .runtime import Browser, Game, SandboxBuilder, looks_like_git
from .tools import Tools

mcp = FastMCP(
    "playtest",
    instructions="Playtest a web game in a Solari cloud browser. Call playtest_open first, or playtest_run for a full automatic pass.",
)

_state: dict[str, Any] = {
    "client": None,
    "browser": None,
    "builder": None,
    "game": None,
    "tools": None,
    "explorer": None,
    "out": None,
}


def _client() -> SolariClient:
    if _state["client"] is None:
        _state["client"] = SolariClient()
    return _state["client"]


def _tools() -> Tools:
    if _state["tools"] is None:
        raise RuntimeError("no game open: call playtest_open first")
    return _state["tools"]


def _game() -> Game:
    return _tools().g


@mcp.tool()
async def playtest_open(
    target: str,
    ref: str | None = None,
    viewport: str = "1280x720",
    build_cmd: str | None = None,
    serve_dir: str = "dist",
) -> dict[str, Any]:
    """Open a game in a Solari cloud browser. `target` is a git URL (built in a Solari sandbox: npm install + vite build, served from `serve_dir`) or a URL that already serves the game. Returns WebGL info, canvas check, and the run folder."""
    await playtest_close()
    w, h = (int(x) for x in viewport.lower().split("x"))
    out = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    url = target
    meta: dict[str, Any] = {"target": target, "viewport": viewport}
    if looks_like_git(target):
        b = SandboxBuilder(_client())
        kw: dict[str, Any] = {"serve_dir": serve_dir}
        if build_cmd:
            kw["build_cmd"] = build_cmd
        served = await b.serve_from_git(target, ref, **kw)
        _state["builder"] = b
        url = served.url
        meta.update(
            {"commit": served.commit, "sandbox": served.sandbox_id, "build_s": round(served.build_s, 1)}
        )
    br = Browser(_client())
    game = await br.open(url, width=w, height=h, run_dir=out)
    _state.update(
        {"browser": br, "game": game, "tools": Tools(game), "explorer": None, "out": out, "meta": meta}
    )
    meta.update(
        {
            "url": url.split("?")[0],
            "webgl": await game.webgl(),
            "canvas": await game.canvas_variance(),
            "fps": await game.fps(0.5),
            "title": await game.evaluate("document.title"),
            "run_dir": str(out),
        }
    )
    return meta


@mcp.tool()
async def playtest_screenshot(label: str = "shot") -> Image:
    """Screenshot of the game as it is now (PNG)."""
    p = await _game().screenshot(label)
    return Image(data=p.read_bytes(), format="png")


@mcp.tool()
async def playtest_press(key: str, repeat: int = 1) -> dict[str, Any]:
    """Press a key: a letter, digit, 'space', 'escape', 'enter', 'arrowleft', ..."""
    return await _tools().press(key, repeat=repeat)


@mcp.tool()
async def playtest_click(
    x: float | None = None,
    y: float | None = None,
    selector: str | None = None,
    text: str | None = None,
    button: str = "left",
) -> dict[str, Any]:
    """Click at coordinates, or the first element matching a CSS selector, or the first button whose text contains `text`."""
    return await _tools().click(x, y, selector=selector, text=text, button=button)


@mcp.tool()
async def playtest_drag(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    """Drag with the left mouse button from (x0,y0) to (x1,y1)."""
    return await _tools().drag(x0, y0, x1, y1)


@mcp.tool()
async def playtest_type(text: str) -> dict[str, Any]:
    """Type text into the focused element."""
    return await _tools().type_text(text)


@mcp.tool()
async def playtest_wait(
    ms: int = 500, until_text: str | None = None, until_selector: str | None = None
) -> dict[str, Any]:
    """Wait a fixed time, or until text appears on the page, or until a selector exists."""
    return await _tools().wait(ms, until_text=until_text, until_selector=until_selector)


@mcp.tool()
async def playtest_ui() -> dict[str, Any]:
    """Visible interactive elements (with centre coordinates) and every panel-like container with its rect and open state. Use this instead of guessing coordinates."""
    t = _tools()
    return {
        "controls": await t.ui_tree(),
        "panels": [p for p in await t.panels() if p["visible"]],
        "text": (await t.text())[:1500],
    }


@mcp.tool()
async def playtest_measure(selector: str) -> dict[str, Any] | None:
    """Rect, computed layout, parent and sibling heights for one element. The tool for 'why is this panel tiny'."""
    return await _tools().measure(selector)


@mcp.tool()
async def playtest_state() -> dict[str, Any]:
    """DOM hash, open panels, console error count, fps and canvas colour count. Diff two of these around an action to see what it changed."""
    s = await _tools().snapshot(with_fps=True, with_canvas=True)
    return s.as_dict()


@mcp.tool()
async def playtest_console() -> list[dict[str, Any]]:
    """Console errors and uncaught exceptions collected since the game was opened."""
    return _tools().console_errors()


@mcp.tool()
async def playtest_smoke() -> dict[str, Any]:
    """Start screen, WebGL, canvas paints, fps, console: the basics, as findings if they fail."""
    ex = _explorer()
    started = await ex.start()
    out = await ex.smoke()
    out["started"] = started
    out["findings"] = [f.as_dict() for f in ex.findings]
    return out


@mcp.tool()
async def playtest_discover(keys: list[str] | None = None, buttons: bool = True) -> list[dict[str, Any]]:
    """Probe keys (all letters, digits, space, escape, arrows by default) and visible buttons from a clean state; returns the ones that open or close panels or change the DOM."""
    ex = _explorer()
    return [c.__dict__ | {"label": c.label()} for c in await ex.discover(keys=keys, buttons=buttons)]


@mcp.tool()
async def playtest_ui_stress(depth: int = 3, max_sequences: int = 80) -> list[dict[str, Any]]:
    """Open/close the discovered panels in every order that breaks real games (pairs, close-and-reopen, mixed triples) and report panels that end up unusable, shrunk, off-screen, or throw console errors."""
    ex = _explorer()
    if not ex.controls:
        await ex.discover()
    return [f.as_dict() for f in await ex.ui_stress(depth=depth, max_sequences=max_sequences)]


@mcp.tool()
async def playtest_report() -> str:
    """Write report.json / report.md / report.html for the current session and return the Markdown."""
    ex = _explorer()
    t = _tools()
    out: Path = _state["out"]
    meta = dict(_state.get("meta") or {})
    meta.setdefault("time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    meta["browser_session"] = t.g.session_id
    run = {
        "meta": meta,
        "controls": [{"label": c.label(), **c.__dict__} for c in ex.controls],
        "sequences": len(t.log.steps),
        "findings": [f.as_dict() for f in ex.findings],
        "actions": t.log.steps,
    }
    write_json(out / "report.json", run)
    write_markdown(out / "report.md", run)
    write_html(out / "report.html", run)
    return (out / "report.md").read_text()


@mcp.tool()
async def playtest_close() -> dict[str, Any]:
    """Release the Solari browser (and sandbox). Returns the replay URL when recording was on."""
    replay = None
    br: Browser | None = _state.get("browser")
    if br:
        sid = await br.close()
        replay = await br.replay_url(sid) if sid else None
    b: SandboxBuilder | None = _state.get("builder")
    if b:
        await b.kill()
    _state.update({"browser": None, "builder": None, "game": None, "tools": None, "explorer": None})
    return {"closed": True, "replay_url": replay}


@mcp.tool()
async def playtest_run(
    target: str,
    ref: str | None = None,
    hints: dict[str, Any] | None = None,
    viewport: str = "1280x720",
    max_sequences: int = 80,
) -> str:
    """The whole deterministic pass in one call: build (if git), open, smoke, discover controls, UI stress, report. Returns the Markdown report; findings carry replayable sequences, evidence and a hypothesis with a falsifier."""
    w, h = (int(x) for x in viewport.lower().split("x"))
    run = await playtest(target, ref=ref, hints=hints, viewport=(w, h), max_sequences=max_sequences)
    return (Path(run["out"]) / "report.md").read_text()


def _explorer() -> Explorer:
    if _state["explorer"] is None:
        _state["explorer"] = Explorer(_tools())
    return _state["explorer"]


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
