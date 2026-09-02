"""One place for how solab looks.

Instrument-panel structure with a phosphor voice, in Solari's own palette: black
ground, one gold accent, heavy horizontal rules instead of boxes, a fixed glyph per command,
hard-edged inverse pills for verdicts. No emoji, ever.
"""

from __future__ import annotations

from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.theme import Theme

# Solari's own tokens (getsolari.com): black ground, gold accent, grey secondary.
ORANGE = "#f5b301"  # accent gold (name kept for callers)
ORANGE_DIM = "#a37700"
GREY = "#939599"
GREY_DIM = "#3b3b3b"
GREEN = "#9bd89a"
RED = "#ff5c5c"
YELLOW = "#f5c66a"
FG = "#e7e7e2"
BG = "#080a0e"

THEME = Theme(
    {
        "accent": f"bold {ORANGE}",
        "accent.dim": ORANGE_DIM,
        "muted": GREY,
        "muted.dim": GREY_DIM,
        "fg": FG,
        "pass": f"bold {GREEN}",
        "fail": f"bold {RED}",
        "warn": f"bold {YELLOW}",
        "key": GREY,
        "value": FG,
        "num": f"bold {FG}",
        "hdr": GREY,
        "rule.line": GREY_DIM,
        "pill.cmd": f"bold {BG} on {ORANGE}",
        "pill.pass": f"bold {BG} on {GREEN}",
        "pill.fail": f"bold {BG} on {RED}",
        "pill.warn": f"bold {BG} on {YELLOW}",
        "pill.dim": f"{GREY}",
        "progress.percentage": ORANGE,
        "progress.elapsed": GREY,
        "bar.complete": ORANGE,
        "bar.finished": ORANGE,
        "bar.pulse": ORANGE_DIM,
    }
)

console = Console(theme=THEME, highlight=False)
err_console = Console(theme=THEME, highlight=False, stderr=True)

# One mark per command. Never emoji.
MARKS = {
    "doctor": "◇",
    "bench": "▮",
    "isolation": "◫",
    "proxy": "◎",
    "sessions": "≡",
    "cost": "◆",
    "replay": "▸",
    "report": "▤",
    "profiles": "▥",
    "mcp": "⌘",
}

QUESTIONS = {
    "doctor": "does this Solari setup work, and what am I allowed to do?",
    "bench": "session lifecycle timings, raw CDP over websocket",
    "isolation": "does a new session inherit the previous session's state?",
    "proxy": "does the egress the wire sees match the proxy the gateway confirmed?",
    "sessions": "what is running now, and what looks leaked?",
    "cost": "what did my sessions cost, from the ledger and the rate card?",
    "replay": "replay for a recorded session",
    "report": "everything into one dark html page",
    "profiles": "stored browser profiles",
}

WORDMARK = "[accent]▮ SOLAB[/accent] [accent.dim]///[/accent.dim]"


def mark(cmd: str) -> str:
    return MARKS.get(cmd, "▪")


def pill(text: str, kind: str = "cmd") -> str:
    """Hard-edged inverse pill. kind: cmd | pass | fail | warn | dim."""
    if kind == "dim":
        return f"[pill.dim]{text}[/pill.dim]"
    return f"[pill.{kind}] {text} [/pill.{kind}]"


def verdict(ok: bool | None, text_ok: str = "PASS", text_fail: str = "FAIL", text_none: str = "SKIP") -> str:
    if ok is None:
        return pill(text_none, "warn")
    return pill(text_ok, "pass") if ok else pill(text_fail, "fail")


def badge(text: str, style: str = "accent") -> str:  # kept for callers
    return pill(text, "cmd")


def header(cmd: str, dry_run: bool = False) -> None:
    q = QUESTIONS.get(cmd, "")
    tail = "  " + pill("DRY RUN", "dim") if dry_run else ""
    console.print(f"{WORDMARK} [bold fg]{cmd.upper()}[/bold fg]  [muted]{q}[/muted]{tail}")
    console.print(Rule(style="rule.line", characters="━"))


def footer(cmd: str, ok: bool | None, labels: tuple[str, str, str], summary: str) -> None:
    console.print(Rule(style="rule.line", characters="━"))
    console.print(f"{pill(cmd.upper(), 'cmd')} {verdict(ok, *labels)}  [muted]{summary}[/muted]")


def table(*columns: tuple[str, str]) -> Table:
    """Borderless table with grey-caps headers. columns: (name, justify)."""
    t = Table(box=None, show_edge=False, pad_edge=True, padding=(0, 2), header_style="hdr", expand=False)
    for name, justify in columns:
        t.add_column(name.upper(), justify=justify)  # type: ignore[arg-type]
    return t


def method(text: str) -> None:
    console.print(f"  [muted.dim]{text}[/muted.dim]")


def kv(key: str, value: str) -> None:
    console.print(f"  [key]{key.upper():<10}[/key] [value]{value}[/value]")


def sparkline(values: list[float]) -> str:
    """Eight-level unicode sparkline, empty string for no data."""
    bars = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return bars[3] * len(values)
    return "".join(bars[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in values)


def ms(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds * 1000:,.0f} ms" if seconds < 10 else f"{seconds:,.1f} s"
