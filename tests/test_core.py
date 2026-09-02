import json
from pathlib import Path

from solari_playtest import explore, report, tools
from solari_playtest.explore import Control, Explorer
from solari_playtest.tools import Snapshot, _key_params


def test_key_params():
    assert _key_params("r") == ("r", "KeyR", "r")
    assert _key_params("3") == ("3", "Digit3", "3")
    assert _key_params("escape") == ("Escape", "Escape", "")
    assert _key_params(" ") == (" ", "Space", " ")
    assert _key_params("arrowleft") == ("ArrowLeft", "ArrowLeft", "")


def _panel(sel, h=400, w=320, open=True, visible=True, inside=True):
    return {
        "sel": sel,
        "title": "",
        "x": 900,
        "y": 70,
        "w": w,
        "h": h,
        "visible": visible,
        "open": open,
        "scrollH": h,
        "clientH": h,
        "inside": inside,
    }


def test_diff_open():
    a = Snapshot(0, "a", [_panel("div.roster", open=False)], 0, "u")
    b = Snapshot(0, "b", [_panel("div.roster", open=True)], 0, "u")
    assert explore._diff_open(a, b) == (["div.roster"], [])
    assert explore._diff_open(b, a) == ([], ["div.roster"])


class FakeGame:
    width, height = 1280, 720
    session_id = "s"

    def __init__(self):
        self.console: list = []

    async def evaluate(self, js):
        return None  # no canvas in the fake


class FakeTools:
    """Scripted game: 'r' toggles the roster; after the mixed sequence it comes back 8 px tall."""

    def __init__(self):
        self.g = FakeGame()
        self.open: dict[str, bool] = {"div.roster": False, "div.log": False, "div.build": False}
        self.pressed: list[str] = []
        self.broken = False
        self.cycled: set[str] = set()
        self.log = tools.ActionLog()
        self.shots = 0

    async def press(self, key, hold_ms=40, repeat=1):
        self.pressed.append(key)
        m = {"r": "div.roster", "l": "div.log", "b": "div.build"}
        if key == "escape":
            for k, o in self.open.items():
                if o and k != "div.roster" and not self.open["div.roster"]:
                    self.cycled.add(k)
                self.open[k] = False
        elif key in m:
            was_open = self.open[m[key]]
            self.open[m[key]] = not was_open
            # the bug: the roster reopens broken once both other panels were opened
            # and closed while it was closed
            if key == "r" and not was_open:
                self.broken = self.cycled >= {"div.log", "div.build"}
                self.cycled = set()
            elif was_open and not self.open["div.roster"]:
                self.cycled.add(m[key])
        return {"pressed": key}

    async def click(self, *a, **k):
        return {"clicked": False}

    async def snapshot(self, **k):
        panels = []
        for sel, is_open in self.open.items():
            h = 8 if (sel == "div.roster" and is_open and self.broken) else 400
            panels.append(_panel(sel, h=h, open=is_open))
        dom = "".join(f"{s}{int(o)}" for s, o in self.open.items())
        return Snapshot(0, dom, panels, 0, "u")

    async def ui_tree(self):
        return []

    async def clickables_in(self, sel, limit=8):
        return []

    async def screenshot(self, label="x"):
        self.shots += 1
        return f"shot-{self.shots}.png"

    async def measure(self, sel):
        return {
            "display": "flex",
            "flex": "1 1 0%",
            "minHeight": "0px",
            "overflow": "visible",
            "parent": "div.rail",
            "siblings": ["div.room 59px", "div.dweller 59px", "div.roster 8px"],
        }


async def test_explorer_discovers_toggles_and_finds_the_shrunk_panel():
    t = FakeTools()
    ex = Explorer(t, settle=0.0)  # type: ignore[arg-type]

    async def fresh():
        t.open = {k: False for k in t.open}
        t.broken = False
        t.cycled = set()

    ex.fresh = fresh
    controls = await ex.discover(keys=["r", "l", "b", "x"], buttons=False)
    labels = {c.label() for c in controls}
    assert labels == {"key:r", "key:l", "key:b"}
    assert all(c.toggles for c in controls)
    assert ex.reference["div.roster"]["h"] == 400
    findings = await ex.ui_stress(depth=3, max_sequences=200)
    titles = [f.title for f in findings]
    assert any("div.roster" in x and "unusably small" in x for x in titles), titles
    f = next(x for x in findings if "unusably small" in x.title)
    keys = [s["action"]["key"] for s in f.sequence]
    assert keys[0] == "r" and keys[-1] == "r" and {"l", "b"} <= set(keys), keys
    assert f.evidence["confirmed_from_fresh_load"] is True
    assert f.severity == "high" and f.screenshots and "flex" in f.hypothesis


async def test_no_findings_on_a_healthy_game():
    t = FakeTools()
    t.press = _healthy_press(t)  # type: ignore[method-assign]
    ex = Explorer(t, settle=0.0)  # type: ignore[arg-type]
    await ex.discover(keys=["r", "l", "b"], buttons=False)
    assert await ex.ui_stress(depth=3, max_sequences=200) == []


def _healthy_press(t):
    async def press(key, hold_ms=40, repeat=1):
        m = {"r": "div.roster", "l": "div.log", "b": "div.build"}
        if key == "escape":
            for k in t.open:
                t.open[k] = False
        elif key in m:
            t.open[m[key]] = not t.open[m[key]]
        return {"pressed": key}

    return press


def test_reports(tmp_path):
    run = {
        "meta": {
            "target": "https://github.com/x/y",
            "commit": "abc123",
            "viewport": "1280x720",
            "time": "t",
            "browser_session": "s",
            "webgl": {"renderer": "llvmpipe"},
            "canvas": {"distinct_colours": 1250},
            "fps": 9.0,
            "replay_url": None,
        },
        "controls": [{"label": "key:r"}],
        "sequences": 12,
        "findings": [
            {
                "id": "F1",
                "severity": "high",
                "title": "Panel div.roster opens unusably small (320×8 px)",
                "sequence": [{"action": {"key": "r"}, "intent": "open"}],
                "evidence": {
                    "panel": _panel("div.roster", h=8),
                    "reference": _panel("div.roster"),
                    "measure": {"flex": "1", "siblings": ["a 59px"]},
                },
                "selectors": ["div.roster"],
                "hypothesis": "flex",
                "falsifier": "height >= 80",
                "screenshots": ["001-x.png"],
            }
        ],
        "actions": [],
    }
    report.write_json(tmp_path / "r.json", run)
    report.write_markdown(tmp_path / "r.md", run)
    report.write_html(tmp_path / "r.html", run)
    md = (tmp_path / "r.md").read_text()
    assert "F1 · HIGH" in md and "press `r`" in md and "Falsifier" in md and "siblings" in md
    html = (tmp_path / "r.html").read_text()
    assert "PLAYTEST" in html and "unusably small" in html and "<script" not in html
    assert json.loads((tmp_path / "r.json").read_text())["findings"][0]["id"] == "F1"


def test_control_label():
    assert Control("key", "r", {"key": "r"}).label() == "key:r"
    assert Path(__file__).exists()
