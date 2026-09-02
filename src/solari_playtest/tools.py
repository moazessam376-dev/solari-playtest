"""Game-agnostic primitives over a live `Game`: input, observation, measurement.

Each function here is also exposed as an MCP tool and as a browser-use tool, so
keep them small, typed, and side-effect explicit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .runtime import Game

# Elements that behave like panels, dialogs or HUD regions in most web games.
PANEL_SELECTOR = "[class*=panel], [class*=menu], [class*=modal], [class*=dialog], [class*=drawer], [class*=sheet], [class*=overlay], [role=dialog], [role=menu], aside, nav"

JS_PANELS = f"""(() => {{
  const vw = innerWidth, vh = innerHeight;
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll({json.dumps(PANEL_SELECTOR)})) {{
    if (seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const visible = cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05 && r.width > 0 && r.height > 0
      && r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh;
    const openClass = el.classList.contains('open') || el.classList.contains('active') || el.classList.contains('visible') || el.classList.contains('show') || el.getAttribute('aria-hidden') === 'false';
    const sel = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].slice(0, 4).map(c => '.' + c).join('');
    const title = (el.querySelector('h1,h2,h3,[class*=title]')?.textContent || '').trim().slice(0, 40);
    out.push({{ sel, title, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      visible, open: openClass, scrollH: el.scrollHeight, clientH: el.clientHeight,
      inside: r.left >= -1 && r.top >= -1 && r.right <= vw + 1 && r.bottom <= vh + 1 }});
  }}
  return out;
}})()"""

JS_TREE = """(() => {
  const vw = innerWidth, vh = innerHeight;
  const items = [];
  const q = 'button, a[href], input, select, textarea, [role=button], [role=tab], [role=menuitem], [tabindex], [onclick], [data-action], [draggable=true]';
  for (const el of document.querySelectorAll(q)) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
    if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    const sel = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].slice(0, 3).map(c => '.' + c).join('');
    items.push({ sel, role: el.getAttribute('role') || el.tagName.toLowerCase(), text, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height), disabled: !!el.disabled });
  }
  return items.slice(0, 200);
})()"""

JS_DOM_HASH = """(() => { let s = ''; const walk = (n, d) => { if (d > 12) return; if (n.nodeType === 1) { const cs = getComputedStyle(n); if (cs.display === 'none') return; s += n.tagName + '|' + n.className + '|'; for (const c of n.children) walk(c, d + 1); } }; walk(document.body, 0); return s; })()"""

KEY_CODES = {
    " ": ("Space", " "),
    "enter": ("Enter", "\r"),
    "escape": ("Escape", ""),
    "tab": ("Tab", "\t"),
    "backspace": ("Backspace", ""),
    "arrowup": ("ArrowUp", ""),
    "arrowdown": ("ArrowDown", ""),
    "arrowleft": ("ArrowLeft", ""),
    "arrowright": ("ArrowRight", ""),
}


def _key_params(key: str) -> tuple[str, str, str]:
    k = key.lower()
    if k in KEY_CODES:
        code, text = KEY_CODES[k]
        name = {
            "enter": "Enter",
            "escape": "Escape",
            "tab": "Tab",
            "backspace": "Backspace",
            " ": " ",
            "arrowup": "ArrowUp",
            "arrowdown": "ArrowDown",
            "arrowleft": "ArrowLeft",
            "arrowright": "ArrowRight",
        }[k]
        return name, code, text
    if len(k) == 1 and k.isalpha():
        return k, f"Key{k.upper()}", k
    if len(k) == 1 and k.isdigit():
        return k, f"Digit{k}", k
    return key, key, key


@dataclass
class Snapshot:
    t: float
    dom_hash: str
    panels: list[dict[str, Any]]
    console_errors: int
    url: str
    fps: float | None = None
    canvas_colours: int | None = None

    def open_panels(self) -> list[dict[str, Any]]:
        return [p for p in self.panels if p["visible"] and (p["open"] or p["w"] * p["h"] > 0)]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionLog:
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, **kw: Any) -> None:
        self.steps.append({"t": time.time(), "kind": kind, **kw})


class Tools:
    def __init__(self, game: Game) -> None:
        self.g = game
        self.log = ActionLog()

    # --- input -----------------------------------------------------------------

    async def press(self, key: str, hold_ms: int = 40, repeat: int = 1) -> dict[str, Any]:
        name, code, text = _key_params(key)
        for _ in range(repeat):
            params: dict[str, Any] = {"type": "keyDown", "key": name, "code": code}
            if text:
                params["text"] = text
            await self.g.send("Input.dispatchKeyEvent", params)
            await asyncio.sleep(hold_ms / 1000)
            await self.g.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": code})
            await asyncio.sleep(0.05)
        self.log.add("press", key=key, repeat=repeat)
        return {"pressed": key, "repeat": repeat}

    async def type_text(self, text: str) -> dict[str, Any]:
        await self.g.send("Input.insertText", {"text": text})
        self.log.add("type", text=text)
        return {"typed": text}

    async def click(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        selector: str | None = None,
        text: str | None = None,
        button: str = "left",
    ) -> dict[str, Any]:
        if selector or text:
            js = f"""(() => {{ const els = [...document.querySelectorAll({json.dumps(selector or "button, a, [role=button], [onclick], [data-action]")})];
              const el = {'els.find(e => (e.innerText||"").trim().toLowerCase().includes(' + json.dumps(text.lower()) + "))" if text else "els[0]"};
              if (!el) return null; el.scrollIntoView({{block:'nearest'}}); const r = el.getBoundingClientRect(); return {{x: r.x + r.width/2, y: r.y + r.height/2, sel: el.tagName.toLowerCase() + '.' + [...el.classList].join('.'), text: (el.innerText||'').trim().slice(0,40)}}; }})()"""
            hit = await self.g.evaluate(js)
            if not hit:
                return {"clicked": False, "reason": f"no element for {selector or text!r}"}
            x, y = hit["x"], hit["y"]
        assert x is not None and y is not None
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            p: dict[str, Any] = {
                "type": t,
                "x": x,
                "y": y,
                "button": button if t != "mouseMoved" else "none",
                "clickCount": 1,
            }
            await self.g.send("Input.dispatchMouseEvent", p)
            await asyncio.sleep(0.03)
        self.log.add("click", x=round(x), y=round(y), selector=selector, text=text, button=button)
        return {"clicked": True, "x": round(x), "y": round(y)}

    async def drag(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> dict[str, Any]:
        await self.g.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y0})
        await self.g.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x0, "y": y0, "button": "left", "clickCount": 1},
        )
        for i in range(1, steps + 1):
            await self.g.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": x0 + (x1 - x0) * i / steps,
                    "y": y0 + (y1 - y0) * i / steps,
                    "button": "left",
                },
            )
            await asyncio.sleep(0.02)
        await self.g.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x1, "y": y1, "button": "left", "clickCount": 1},
        )
        self.log.add("drag", frm=[round(x0), round(y0)], to=[round(x1), round(y1)])
        return {"dragged": True}

    async def scroll(self, x: float, y: float, dy: float) -> dict[str, Any]:
        await self.g.send(
            "Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy}
        )
        self.log.add("scroll", x=round(x), y=round(y), dy=dy)
        return {"scrolled": dy}

    async def wait(
        self,
        ms: int = 500,
        *,
        until_text: str | None = None,
        until_selector: str | None = None,
        timeout_ms: int = 10_000,
    ) -> dict[str, Any]:
        if until_text or until_selector:
            t0 = time.time()
            js = (
                f"!!(document.querySelector({json.dumps(until_selector)}))"
                if until_selector
                else f"document.body.innerText.toLowerCase().includes({json.dumps(until_text.lower())})"
            )
            while time.time() - t0 < timeout_ms / 1000:
                if await self.g.evaluate(js):
                    return {"waited_ms": round((time.time() - t0) * 1000), "found": True}
                await asyncio.sleep(0.2)
            return {"waited_ms": timeout_ms, "found": False}
        await asyncio.sleep(ms / 1000)
        return {"waited_ms": ms}

    # --- observation ---------------------------------------------------------------

    async def screenshot(self, label: str = "shot") -> str:
        return str(await self.g.screenshot(label))

    async def panels(self) -> list[dict[str, Any]]:
        return await self.g.evaluate(JS_PANELS) or []

    async def ui_tree(self) -> list[dict[str, Any]]:
        return await self.g.evaluate(JS_TREE) or []

    async def clickables_in(self, container_sel: str, limit: int = 8) -> list[dict[str, Any]]:
        """Clickable-looking descendants of a container: buttons, rows, and anything
        with cursor:pointer. Used to find controls that live inside panels."""
        js = rf"""(() => {{ const root = document.querySelector({json.dumps(container_sel)}); if (!root) return [];
          const out = []; const seen = new Set();
          for (const el of root.querySelectorAll('*')) {{
            if (out.length >= {limit}) break;
            const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
            if (r.width < 8 || r.height < 8 || cs.display === 'none' || cs.visibility === 'hidden') continue;
            const clicky = cs.cursor === 'pointer' || el.tagName === 'BUTTON' || el.hasAttribute('draggable') || /row|item|card|entry/.test(el.className);
            if (!clicky) continue;
            if ([...seen].some(p => p.contains(el))) continue;  // keep the outermost clickable
            seen.add(el);
            const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
            if (/^[✕×x]$/.test(text)) continue;
            out.push({{ sel: el.tagName.toLowerCase() + [...el.classList].slice(0, 3).map(c => '.' + c).join(''), text, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) }});
          }}
          return out; }})()"""
        return await self.g.evaluate(js) or []

    async def measure(self, selector: str) -> dict[str, Any] | None:
        js = f"""(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return null; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
          const p = el.parentElement; const pr = p ? p.getBoundingClientRect() : null;
          return {{ x: r.x, y: r.y, w: r.width, h: r.height, display: cs.display, visibility: cs.visibility, opacity: cs.opacity, overflow: cs.overflow, flex: cs.flex, minHeight: cs.minHeight,
                   scrollH: el.scrollHeight, clientH: el.clientHeight, clippedByParent: pr ? (r.bottom > pr.bottom + 1 || r.top < pr.top - 1) : false, parent: p ? p.tagName.toLowerCase() + '.' + [...p.classList].join('.') : null,
                   siblings: p ? [...p.children].map(c => c.tagName.toLowerCase() + '.' + [...c.classList].slice(0,3).join('.') + ' ' + Math.round(c.getBoundingClientRect().height) + 'px') : [] }}; }})()"""
        return await self.g.evaluate(js)

    async def text(self) -> str:
        return str(await self.g.evaluate("document.body.innerText.slice(0, 4000)") or "")

    async def snapshot(self, *, with_fps: bool = False, with_canvas: bool = False) -> Snapshot:
        dom = hashlib.sha1(str(await self.g.evaluate(JS_DOM_HASH) or "").encode()).hexdigest()[:12]
        panels = await self.panels()
        snap = Snapshot(
            time.time(),
            dom,
            panels,
            len([c for c in self.g.console if c.level != "warning"]),
            str(await self.g.evaluate("location.href")),
        )
        if with_fps:
            snap.fps = await self.g.fps(0.5) * 1
        if with_canvas:
            snap.canvas_colours = (await self.g.canvas_variance()).get("distinct_colours")
        return snap

    def console_errors(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.g.console]
