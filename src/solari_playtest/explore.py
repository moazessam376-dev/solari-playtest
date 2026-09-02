"""Deterministic explorers. No model in the loop: probe what the controls do,
then stress the UI with the sequences that break real games, and turn every
invariant violation into a finding with evidence.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

from .tools import PANEL_SELECTOR, Snapshot, Tools

PROBE_KEYS = [
    *"abcdefghijklmnopqrstuvwxyz",
    *"0123456789",
    " ",
    "escape",
    "enter",
    "tab",
    "arrowup",
    "arrowdown",
    "arrowleft",
    "arrowright",
]
MIN_USABLE = 80  # px: a panel shorter or narrower than this cannot be used
SETTLE = 0.45  # s: CSS transitions in most games finish well within this


@dataclass
class Control:
    kind: str  # key | button
    name: str  # the key, or the button text
    action: dict[str, Any]  # how to trigger it: {"key": "r"} or {"click_text": "..."} or {"click_xy": [x, y]}
    opens: list[str] = field(default_factory=list)  # panel selectors it opens
    closes: list[str] = field(default_factory=list)
    toggles: bool = False
    dom_change: bool = False

    def label(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class Finding:
    id: str
    severity: str  # high | medium | low
    title: str
    sequence: list[dict[str, Any]]
    evidence: dict[str, Any]
    selectors: list[str]
    hypothesis: str
    falsifier: str
    screenshots: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


def _open_map(snap: Snapshot) -> dict[str, dict[str, Any]]:
    return {p["sel"]: p for p in snap.panels if p["visible"] and p["open"]}


def _diff_open(before: Snapshot, after: Snapshot) -> tuple[list[str], list[str]]:
    b, a = set(_open_map(before)), set(_open_map(after))
    return sorted(a - b), sorted(b - a)


class Explorer:
    def __init__(
        self, tools: Tools, *, hints: dict[str, Any] | None = None, on_progress=None, settle: float = SETTLE
    ) -> None:
        self.t = tools
        self.settle = settle
        self.hints = hints or {}
        self.findings: list[Finding] = []
        self.controls: list[Control] = []
        self.reference: dict[str, dict[str, Any]] = {}  # panel -> rect when opened alone from clean state
        self.progress = on_progress or (lambda *_: None)
        self._n = 0
        self.fresh = None  # async callable: reload the game and pass the start screen
        self.history: list[dict[str, Any]] = []  # every action since the last fresh load
        self._confirming = False

    # --- helpers -----------------------------------------------------------------

    async def _do(self, action: dict[str, Any]) -> None:
        self.history.append({"action": action})
        if "steps" in action:  # composite: e.g. open a panel, then click a row inside it
            for step in action["steps"]:
                await self._do_one(step)
            return
        await self._do_one(action)

    async def _do_one(self, action: dict[str, Any]) -> None:
        if "key" in action:
            await self.t.press(action["key"])
        elif "click_text" in action:
            await self.t.click(text=action["click_text"])
        elif "click_sel" in action:
            r = await self.t.click(selector=action["click_sel"])
            if not r.get("clicked") and "click_xy" in action:
                await self.t.click(action["click_xy"][0], action["click_xy"][1])
        elif "click_xy" in action:
            await self.t.click(action["click_xy"][0], action["click_xy"][1])
        elif "close_panel" in action:
            await self.t.g.evaluate(
                "(() => { const el = document.querySelector("
                + json.dumps(
                    action["close_panel"].split("#")[0].split(".", 1)[0]
                    + "."
                    + ".".join(action["close_panel"].split(".")[1:])
                    if "." in action["close_panel"]
                    else action["close_panel"]
                )
                + ");"
                " const b = el && el.querySelector('.x-btn, [class*=close], button[aria-label*=lose], [data-close]'); if (b) b.click(); })()"
            )
        await asyncio.sleep(self.settle)

    async def reset(self) -> Snapshot:
        """Close everything. Escape first; if a panel survives or escape itself
        opened something (pause menus do that), click the close control inside
        each open panel; give up after a few rounds and report what is left."""
        prev: set[str] | None = None
        snap = await self.t.snapshot()
        for _ in range(4):
            open_now = set(_open_map(snap))
            if not open_now:
                return snap
            if prev is not None and open_now >= prev:
                # escape did not help (or made it worse): close from inside the panels
                await self.t.g.evaluate(
                    "(() => { for (const el of document.querySelectorAll("
                    + json.dumps(PANEL_SELECTOR)
                    + ")) {"
                    " if (!el.classList.contains('open')) continue;"
                    " const b = el.querySelector('.x-btn, [class*=close], button[aria-label*=lose], [data-close]');"
                    " if (b) b.click(); } })()"
                )
                await asyncio.sleep(self.settle * 0.7)
            else:
                await self.t.press("escape")
                await asyncio.sleep(self.settle * 0.7)
            prev = open_now
            snap = await self.t.snapshot()
        return snap

    def _finding(
        self,
        sev: str,
        title: str,
        seq: list[dict[str, Any]],
        evidence: dict[str, Any],
        selectors: list[str],
        hypothesis: str,
        falsifier: str,
        shots: list[str],
        confirmed: bool | None = None,
    ) -> None:
        key = (title.split(" (")[0], tuple(selectors))
        existing = next(
            (f for f in self.findings if (f.title.split(" (")[0], tuple(f.selectors)) == key), None
        )
        if existing is not None:
            if confirmed and not existing.evidence.get("confirmed_from_fresh_load"):
                # a short, reproducible sequence beats a long state-dependent one
                existing.title, existing.sequence, existing.evidence, existing.screenshots = (
                    title,
                    seq,
                    evidence,
                    shots,
                )
                existing.evidence["confirmed_from_fresh_load"] = True
            return
        self._n += 1
        f = Finding(f"F{self._n}", sev, title, list(seq), evidence, selectors, hypothesis, falsifier, shots)
        f.evidence["confirmed_from_fresh_load"] = confirmed
        self.findings.append(f)
        self.progress("finding", title)

    async def _confirm(self, seq: list[dict[str, Any]], check) -> tuple[bool, list[dict[str, Any]]]:
        """Reload the game and replay `seq` alone. Returns (reproduced, sequence to
        report): the short sequence when it reproduces, else the full history since
        the last fresh load, which is the honest repro."""
        history = list(self.history)
        if self.fresh is None or self._confirming:
            return True, list(seq)
        self._confirming = True
        try:
            await self.fresh()
            self.history = []
            await self.reset()
            for step in seq:
                await self._do(step["action"])
            ok = await check()
            return (True, seq) if ok else (False, history)
        finally:
            self._confirming = False

    # --- start screen --------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Get past a title screen if there is one: hinted button text, else the
        largest visible button whose text looks like start/play/begin/open/continue."""
        tree = await self.t.ui_tree()
        want = [self.hints.get("start_button")] if self.hints.get("start_button") else []
        words = ("start", "play", "begin", "open", "continue", "new game", "enter", "launch")
        cands = [
            b
            for b in tree
            if b["text"]
            and (any(w in b["text"].lower() for w in want if w) or any(w in b["text"].lower() for w in words))
        ]
        if not cands:
            return {"started": False, "reason": "no start-like button"}
        best = max(cands, key=lambda b: b["w"] * b["h"])
        await self.t.click(best["x"], best["y"])
        await asyncio.sleep(self.settle * 3 + 0.2)
        return {"started": True, "button": best["text"]}

    # --- discovery ---------------------------------------------------------------------

    async def discover(self, keys: list[str] | None = None, buttons: bool = True) -> list[Control]:
        """Press every key and click every visible button once from a clean state;
        keep the ones that open or close a panel or change the DOM."""
        base = await self.reset()
        keys = keys or self.hints.get("keys") or PROBE_KEYS
        for k in keys:
            await self.t.press(k)
            await asyncio.sleep(self.settle)
            after = await self.t.snapshot()
            opened, closed = _diff_open(base, after)
            changed = after.dom_hash != base.dom_hash
            if opened or closed or changed:
                c = Control("key", k, {"key": k}, opened, closed, dom_change=changed)
                # is it a toggle? press again and see if it undoes
                await self.t.press(k)
                await asyncio.sleep(self.settle)
                again = await self.t.snapshot()
                c.toggles = bool(opened) and not _open_map(again).keys() & set(opened)
                if opened:
                    for sel in opened:
                        self.reference.setdefault(sel, _open_map(after)[sel])
                self.controls.append(c)
                self.progress("control", c.label())
            base = await self.reset()
        if buttons:
            for b in await self.t.ui_tree():
                if not b["text"] or b["disabled"] or b["text"] in ("✕", "×"):
                    continue
                await self.t.click(b["x"], b["y"])
                await asyncio.sleep(self.settle)
                after = await self.t.snapshot()
                opened, closed = _diff_open(base, after)
                if opened:
                    c = Control("button", b["text"], {"click_text": b["text"]}, opened, closed)
                    for sel in opened:
                        self.reference.setdefault(sel, _open_map(after)[sel])
                    c.toggles = True
                    self.controls.append(c)
                    self.progress("control", c.label())
                base = await self.reset()
        # canvas hotspots: games draw their world in WebGL, so click a grid of points
        # and keep the first point that opens each distinct panel
        rect = await self.t.g.evaluate(
            "(() => { const c = document.querySelector('canvas'); if (!c) return null; const r = c.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; })()"
        )
        if rect and rect["w"] > 200 and rect["h"] > 200:
            cols, rows = 7, 5
            seen_panels: set[str] = {o for c in self.controls for o in c.opens}
            for j in range(rows):
                for i in range(cols):
                    x = rect["x"] + rect["w"] * (i + 0.5) / cols
                    y = rect["y"] + rect["h"] * (j + 0.5) / rows
                    await self.t.click(x, y)
                    await asyncio.sleep(self.settle)
                    after = await self.t.snapshot()
                    opened, _ = _diff_open(base, after)
                    new = [o for o in opened if o not in seen_panels]
                    if new:
                        c = Control(
                            "canvas", f"canvas@({int(x)},{int(y)})", {"click_xy": [int(x), int(y)]}, new, []
                        )
                        for sel in new:
                            self.reference.setdefault(sel, _open_map(after)[sel])
                            seen_panels.add(sel)
                        self.controls.append(c)
                        self.progress("control", c.label())
                    if opened:
                        base = await self.reset()
        # nested: things inside an open panel that open another panel (rows, cards)
        for parent in list(self.controls):
            if not parent.opens:
                continue
            await self._do(parent.action)
            with_parent = await self.t.snapshot()
            items = []
            for sel in parent.opens:
                items += await self.t.clickables_in(sel, limit=10)
            for it in items:
                await self.t.click(it["x"], it["y"])
                await asyncio.sleep(self.settle)
                after = await self.t.snapshot()
                opened, _ = _diff_open(with_parent, after)
                opened = [o for o in opened if o not in parent.opens]
                if opened and not any(o in c.opens for c in self.controls for o in opened):
                    name = f"{parent.name} › {it['text'] or it['sel']}"
                    c = Control(
                        "nested",
                        name,
                        {"steps": [parent.action, {"click_sel": it["sel"], "click_xy": [it["x"], it["y"]]}]},
                        opened,
                        [],
                    )
                    c.toggles = False
                    for sel in opened:
                        self.reference.setdefault(sel, _open_map(after)[sel])
                    self.controls.append(c)
                    self.progress("control", c.label())
                await self._do(parent.action)  # re-open parent in case the click closed it
                await self.t.press("escape")
                await asyncio.sleep(self.settle * 0.5)
                await self._do(parent.action)
            await self.reset()
        return self.controls

    # --- invariants ---------------------------------------------------------------------

    async def _check(self, seq: list[dict[str, Any]], focus: str | None, before_errors: int) -> None:
        snap = await self.t.snapshot()
        open_now = _open_map(snap)
        for sel, p in open_now.items():
            ref = self.reference.get(sel)
            shots: list[str] = []
            if p["h"] < MIN_USABLE or p["w"] < MIN_USABLE:
                shots.append(await self.t.screenshot(f"unusable {sel}"))
                m = await self.t.measure(
                    sel.split("#")[0].split(".", 1)[0] + "." + ".".join(sel.split(".")[1:])
                    if "." in sel
                    else sel
                )

                async def still_small(sel=sel) -> bool:
                    now = _open_map(await self.t.snapshot()).get(sel)
                    return bool(now and (now["h"] < MIN_USABLE or now["w"] < MIN_USABLE))

                reproduced, seq_out = await self._confirm(seq, still_small)
                self._finding(
                    "high",
                    f"Panel {sel} opens unusably small ({p['w']}×{p['h']} px)"
                    + ("" if reproduced else " (state-dependent: needs the full sequence below)"),
                    seq_out,
                    {"panel": p, "reference": ref, "measure": m, "open_panels": list(open_now.values())},
                    [sel],
                    f"{sel} shares a flex column with sibling panels; after siblings were opened and closed in this order it is laid out at {p['h']} px instead of ~{ref['h'] if ref else '?'} px, so the layout keeps stale size from the sibling panels (likely `flex`/`min-height: 0` on the column with collapsed siblings still holding space).",
                    f"Reproduce the sequence and read {sel}'s bounding rect: if its height is ≥ {MIN_USABLE} px the finding is wrong.",
                    shots,
                    confirmed=reproduced,
                )
            elif ref and focus == sel and p["h"] < ref["h"] * 0.8:
                shots.append(await self.t.screenshot(f"shrunk {sel}"))
                self._finding(
                    "medium",
                    f"Panel {sel} reopens at {p['h']} px, was {ref['h']} px when opened alone",
                    seq,
                    {"panel": p, "reference": ref, "open_panels": list(open_now.values())},
                    [sel],
                    f"{sel} does not return to its own size after other panels in the same rail were opened and closed; the rail's layout retains space for closed siblings.",
                    f"Reproduce and compare heights: if {sel} is within 20% of {ref['h']} px the finding is wrong.",
                    shots,
                )
            if not p["inside"]:
                shots.append(await self.t.screenshot(f"offscreen {sel}"))
                self._finding(
                    "medium",
                    f"Panel {sel} is open but partly outside the viewport",
                    seq,
                    {"panel": p},
                    [sel],
                    f"{sel} is positioned at ({p['x']},{p['y']}) size {p['w']}×{p['h']} in a {self.t.g.width}×{self.t.g.height} viewport.",
                    "Reproduce and check the rect is fully inside the viewport.",
                    shots,
                )
        errs = self.t.g.console
        if len([e for e in errs if e.level != "warning"]) > before_errors:
            new = [e.text for e in errs[before_errors:] if e.level != "warning"]
            self._finding(
                "medium",
                f"Console error after sequence: {new[0][:80]}",
                seq,
                {"console": new},
                [],
                "An input sequence triggers an uncaught error or console.error.",
                "Reproduce the sequence with the console open; no new error means the finding is wrong.",
                [await self.t.screenshot("console error")],
            )

    # --- stress ------------------------------------------------------------------------

    async def ui_stress(self, depth: int = 3, max_sequences: int = 80) -> list[Finding]:
        toggles = [c for c in self.controls if c.opens]
        if not toggles:
            return self.findings
        seqs: list[list[Control | str]] = []
        for a in toggles:
            seqs.append([a])
        for a, b in itertools.permutations(toggles, 2):
            seqs.append([a, b])  # open two
            seqs.append([a, b, "~" + a.label(), a])  # open two, close first, reopen first
            seqs.append([a, b, "~" + b.label(), "~" + a.label(), a])  # open two, close both, reopen first
        if depth >= 3:
            for a, b, c in itertools.permutations(toggles, 3):
                # open A, open B, close A, open C, close C, close B, reopen A: A must come back whole
                seqs.append([a, b, "~" + a.label(), c, "~" + c.label(), "~" + b.label(), a])
                # A shares with B, collapse both, cycle B and C, reopen A: the "mixed behaviour" case
                seqs.append(
                    [a, b, "~" + a.label(), "~" + b.label(), b, c, "~" + b.label(), "~" + c.label(), a]
                )
        # the mixed triples are where real layouts break; run them first so a cap never drops them
        seqs = sorted(seqs, key=lambda q: -len(q))[:max_sequences]
        by_label = {c.label(): c for c in toggles}
        for i, seq in enumerate(seqs):
            self.progress("sequence", f"{i + 1}/{len(seqs)}")
            if self.fresh is not None and i % 8 == 0:
                await self.fresh()
                self.history = []
            await self.reset()
            errors_before = len([e for e in self.t.g.console if e.level != "warning"])
            steps: list[dict[str, Any]] = []
            focus: str | None = None
            for item in seq:
                if isinstance(item, str):  # "~key:r" close via its own toggle
                    ctl = by_label[item[1:]]
                    if ctl.toggles:
                        await self._do(ctl.action)
                        steps.append({"action": ctl.action, "intent": f"close via {ctl.label()}"})
                    else:
                        await self._do({"close_panel": ctl.opens[0]})
                        steps.append(
                            {"action": {"close_panel": ctl.opens[0]}, "intent": f"close {ctl.opens[0]}"}
                        )
                    focus = None
                else:
                    await self._do(item.action)
                    steps.append(
                        {"action": item.action, "intent": f"open {', '.join(item.opens)} via {item.label()}"}
                    )
                    focus = item.opens[0] if item.opens else None
                await self._check(steps, focus, errors_before)
        return self.findings

    # --- smoke -------------------------------------------------------------------------

    async def smoke(self) -> dict[str, Any]:
        g = self.t.g
        out: dict[str, Any] = {
            "webgl": await g.webgl(),
            "canvas": await g.canvas_variance(),
            "fps": await g.fps(1.0),
            "title": await g.evaluate("document.title"),
        }
        if out["canvas"].get("canvas") and out["canvas"].get("distinct_colours", 0) <= 2:
            self._finding(
                "high",
                "The canvas paints a flat colour",
                [],
                out,
                ["canvas"],
                "The WebGL scene does not render (context lost, shader failure, or software GL unsupported feature).",
                "A screenshot of the canvas with more than two distinct colours proves it renders.",
                [await self.t.screenshot("flat canvas")],
            )
        if out["fps"] and out["fps"] < 15:
            self._finding(
                "low",
                f"Frame rate is {out['fps']:.0f} fps on software WebGL",
                [],
                {"fps": out["fps"], "renderer": out["webgl"].get("renderer")},
                ["canvas"],
                "The scene is heavy for a CPU renderer; on real GPUs it may be fine, but this is what a cloud browser or a weak laptop sees.",
                "A run on a GPU-backed browser above 30 fps limits the finding to software GL.",
                [],
            )
        errs = [e for e in g.console if e.level != "warning"]
        if errs:
            self._finding(
                "medium",
                f"{len(errs)} console error(s) during load and idle",
                [],
                {"console": [e.text for e in errs][:10]},
                [],
                "The game logs errors before any input.",
                "A clean console on load means the finding is wrong.",
                [],
            )
        out["console_errors"] = len(errs)
        return out
