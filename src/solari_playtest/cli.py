"""`playtest`: the command line."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.rule import Rule

from . import __version__
from .client import SolariError
from .run import load_hints, playtest
from .theme import console, err_console, pill

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    help="Playtest a web game on Solari and report to your coding agent.",
)
WORDMARK = "[accent]▮ PLAYTEST[/accent] [accent.dim]///[/accent.dim]"


def _progress(kind: str, text: str) -> None:
    marks = {
        "build": "◇",
        "open": "▸",
        "start": "▸",
        "discover": "◎",
        "control": "·",
        "stress": "◫",
        "sequence": "·",
        "finding": "◆",
    }
    style = "fail" if kind == "finding" else "muted"
    console.print(f"  [accent]{marks.get(kind, '·')}[/accent] [{style}]{kind:<9}[/{style}] {text}")


@app.command()
def run(
    target: str = typer.Argument(
        ..., help="Git URL (built in a Solari sandbox) or a URL that already serves the game."
    ),
    ref: str | None = typer.Option(None, "--ref", help="Branch, tag or commit."),
    hints: Path | None = typer.Option(
        None, "--hints", help="playtest.yaml with keys, start_button, build_cmd, serve_dir."
    ),
    viewport: str = typer.Option("1280x720", "--viewport"),
    out: Path | None = typer.Option(None, "--out"),
    depth: int = typer.Option(3, "--depth", min=1, max=3),
    max_sequences: int = typer.Option(80, "--max-sequences"),
    build_cmd: str | None = typer.Option(None, "--build-cmd"),
    keep_sandbox: bool = typer.Option(False, "--keep-sandbox"),
    agent: bool = typer.Option(
        False, "--agent", help="Also run the autonomous browser-use pass (needs an LLM key)."
    ),
    agent_steps: int = typer.Option(60, "--agent-steps"),
) -> None:
    """Build, play, stress the UI, and write report.json / report.md / report.html."""
    console.print()
    console.print(f"{WORDMARK} [bold fg]RUN[/bold fg]  [muted]{target}[/muted]")
    console.print(Rule(style="rule.line", characters="━"))
    w, h = (int(x) for x in viewport.lower().split("x"))
    try:
        result = asyncio.run(
            playtest(
                target,
                ref=ref,
                hints=load_hints(hints),
                viewport=(w, h),
                out=out,
                depth=depth,
                max_sequences=max_sequences,
                build_cmd=build_cmd,
                keep_sandbox=keep_sandbox,
                agent=agent,
                agent_steps=agent_steps,
                progress=_progress,
            )
        )
    except SolariError as err:
        err_console.print(f"[fail]error[/fail] {err}")
        raise typer.Exit(2)
    f = result["findings"]
    console.print(Rule(style="rule.line", characters="━"))
    high = sum(1 for x in f if x["severity"] == "high")
    verdict = pill("CLEAN", "pass") if not f else pill(f"{len(f)} FINDINGS", "fail" if high else "warn")
    console.print(
        f"{pill('PLAYTEST', 'cmd')} {verdict}  [muted]{result['out']}/report.md · replay {result['meta'].get('replay_url') and 'available' or 'n/a'}[/muted]"
    )
    for x in f:
        console.print(
            f"  [{'fail' if x['severity'] == 'high' else 'warn'}]{x['severity']:<6}[/{'fail' if x['severity'] == 'high' else 'warn'}] {x['id']} {x['title']}"
        )
    raise typer.Exit(1 if high else 0)


@app.command()
def replay(report: Path = typer.Argument(..., help="report.json from a run")) -> None:
    """Print the findings of a saved run."""
    run_ = json.loads(report.read_text())
    for x in run_["findings"]:
        console.print(f"[accent]{x['id']}[/accent] {x['severity']} {x['title']}")
        for i, s in enumerate(x["sequence"], 1):
            console.print(f"   {i}. {json.dumps(s['action'])}")


@app.command()
def version() -> None:
    print(f"playtest {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
