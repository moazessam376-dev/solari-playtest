"""The autonomous runner: a browser-use agent on a Solari browser, with the
playtest primitives registered as tools so it can measure, not only look.

    playtest run <target> --agent

Needs `pip install solari-playtest[agent]` and an LLM key (GOOGLE_API_KEY works
on the free tier; ANTHROPIC_API_KEY or OPENAI_API_KEY if you have them).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SYSTEM = """You are a professional game QA playtester working inside a cloud browser.
Goal: understand this web game, then break it. Work in this order:
1. Read what is on screen (use the ui tool: it lists controls with coordinates and panels with sizes). Find the start button and any on-screen help or key hints.
2. Learn the controls: try the hinted keys and every visible button once; note what each opens or changes.
3. Play each mechanic once so you understand the loop.
4. Try mixed behaviours: open two panels that share a side, collapse one, open others, reopen the first; press keys while a dialog is open; act during a transition; resize expectations; rapid repeated toggles.
5. After every action call the state tool and compare: a panel that is open but smaller than 80 px, a panel outside the viewport, a console error, a control that stopped responding, a number in the HUD that moved the wrong way, all count as findings.
6. Before reporting a finding, reproduce it once more from a fresh reload with the shortest sequence you can. Use the measure tool on the affected element to gather computed layout and sibling sizes.
When done, call the done action with a JSON list of findings: [{"severity": "high|medium|low", "title": "...", "sequence": ["press r", "click 'Auto-assign'", ...], "evidence": "...", "hypothesis": "...", "falsifier": "..."}]. Report nothing you could not reproduce. Never invent bugs."""


async def run_agent(
    target_url: str,
    *,
    hints: dict[str, Any] | None,
    out: Path,
    max_steps: int = 60,
    viewport: tuple[int, int] = (1280, 720),
) -> dict[str, Any]:
    from browser_use import Agent
    from browser_use import Tools as BUTools
    from solari_browser_use import SolariBrowser, plain_logs

    from .tools import Tools

    plain_logs()
    llm, fallback = _llm()
    browser = SolariBrowser(recording=True, viewport={"width": viewport[0], "height": viewport[1]})
    bu = BUTools()
    holder: dict[str, Any] = {}

    async def _t() -> Tools:
        if "tools" not in holder:
            # browser-use has its own CDP client; open a second one on the same browser
            from .cdp import CDP
            from .runtime import Game

            c = CDP(browser.solari_cdp_url, timeout_s=30)
            await c.connect()
            targets = await c.send("Target.getTargets")
            page_targets = [t for t in targets.get("targetInfos", []) if t.get("type") == "page"]
            att = await c.send(
                "Target.attachToTarget", {"targetId": page_targets[0]["targetId"], "flatten": True}
            )
            g = Game(
                browser.solari_session_id or "",
                c,
                att["sessionId"],
                target_url,
                viewport[0],
                viewport[1],
                run_dir=out,
            )
            await c.send("Runtime.enable", session_id=g.page)
            holder["tools"] = Tools(g)
        return holder["tools"]

    @bu.action(
        "Read the UI: interactive controls with centre coordinates, panel-like containers with rects and open state, and visible text."
    )
    async def ui() -> str:
        t = await _t()
        return json.dumps(
            {
                "controls": (await t.ui_tree())[:60],
                "panels": [p for p in await t.panels() if p["visible"]],
                "text": (await t.text())[:1200],
            }
        )

    @bu.action(
        "Snapshot of the game state: DOM hash, open panels with sizes, console error count, fps, canvas colour count. Call before and after an action and compare."
    )
    async def state() -> str:
        t = await _t()
        return json.dumps((await t.snapshot(with_fps=True, with_canvas=True)).as_dict())

    @bu.action("Measure one element by CSS selector: rect, computed layout, parent and sibling heights.")
    async def measure(selector: str) -> str:
        t = await _t()
        return json.dumps(await t.measure(selector))

    @bu.action("Press a keyboard key by name (a letter, digit, 'space', 'escape', 'enter', 'arrowleft').")
    async def press_key(key: str) -> str:
        t = await _t()
        return json.dumps(await t.press(key))

    @bu.action("Console errors and exceptions collected so far.")
    async def console_errors() -> str:
        t = await _t()
        return json.dumps(t.console_errors()[-20:])

    task = SYSTEM + "\n\nThe game is already open at " + target_url
    if hints:
        task += "\nHints from the developer (verify them, do not trust blindly): " + json.dumps(hints)
    agent = Agent(
        task=task, llm=llm, fallback_llm=fallback, browser=browser, tools=bu, max_actions_per_step=3
    )
    t0 = time.time()
    history = await agent.run(max_steps=max_steps)
    final = history.final_result() or ""
    findings: list[dict[str, Any]] = []
    try:
        parsed = json.loads(final[final.index("[") : final.rindex("]") + 1]) if "[" in final else []
        findings = [f for f in parsed if isinstance(f, dict) and f.get("title")]
    except (ValueError, json.JSONDecodeError):
        pass
    replay = None
    try:
        replay = await browser.solari_replay_url(timeout_s=45)
    except Exception:  # noqa: BLE001
        pass
    return {
        "meta": {
            "target": target_url,
            "mode": "agent",
            "steps": len(history.history),
            "seconds": round(time.time() - t0),
            "replay_url": replay,
            "model": getattr(llm, "model", str(llm)),
        },
        "findings": [
            {
                "id": f"A{i + 1}",
                "severity": f.get("severity", "medium"),
                "title": f["title"],
                "sequence": [{"action": {"text": s}, "intent": ""} for s in f.get("sequence", [])],
                "evidence": {"agent": f.get("evidence", "")},
                "selectors": [],
                "hypothesis": f.get("hypothesis", ""),
                "falsifier": f.get("falsifier", ""),
                "screenshots": [],
            }
            for i, f in enumerate(findings)
        ],
        "final": final,
    }


def _llm():
    if os.environ.get("GOOGLE_API_KEY"):
        from browser_use import ChatGoogle

        return ChatGoogle(model="gemini-flash-latest"), ChatGoogle(model="gemini-flash-lite-latest")
    if os.environ.get("ANTHROPIC_API_KEY"):
        from browser_use import ChatAnthropic

        return ChatAnthropic(model="claude-sonnet-5"), None
    if os.environ.get("OPENAI_API_KEY"):
        from browser_use import ChatOpenAI

        return ChatOpenAI(model="gpt-4.1-mini"), None
    raise SystemExit("the agent needs GOOGLE_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY")
