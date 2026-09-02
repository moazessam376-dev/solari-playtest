# solari-playtest

A playtest agent for web games, running on [Solari](https://getsolari.com). It builds your game from git in a Solari sandbox, plays it in a Solari cloud Chrome with real WebGL, learns the controls, stress-tests the UI in the orders that break real games, and hands your coding agent a report it can act on.

```bash
pip install solari-playtest
export SOLARI_API_KEY=slr_live_...
export GITHUB_TOKEN=...            # only for private repos
playtest run https://github.com/you/your-game
```

## What it finds without being told

The deterministic pass needs no model and no hints:

1. **Smoke**: start screen, WebGL renderer, whether the canvas actually paints, frame rate, console errors on load.
2. **Discover**: presses every key and clicks every visible control from a clean state, keeps the ones that open or close a panel or change the DOM, then looks inside each open panel for rows and cards that open more.
3. **UI stress**: opens the discovered panels in every order that breaks layouts: pairs, close-and-reopen, and mixed triples such as "open A, share it with B, collapse, cycle B and C, reopen A". After every step it checks that every open panel is still usable (at least 80 px each way, inside the viewport), that a reopened panel comes back to its own size, and that nothing threw.
4. **Confirm**: a suspected bug is replayed from a fresh load. If it reproduces, the report carries the short sequence. If it only happens after earlier interactions, the report says so and carries the full history.

Every finding has a replayable sequence, evidence (rects, computed layout, sibling heights, screenshots), a hypothesis, and a falsifier: what would be observed if the hypothesis were wrong.

## For your coding agent

`playtest-mcp` exposes everything as MCP tools. Add to `.mcp.json`:

```json
{"mcpServers": {"playtest": {"command": "playtest-mcp", "env": {"SOLARI_API_KEY": "slr_live_...", "GITHUB_TOKEN": "..."}}}}
```

Then in `CLAUDE.md` (or the equivalent for Codex or Cursor):

```
Before declaring a UI change done, call playtest_run on this repo and fix every finding with severity high or medium. Re-run to confirm the falsifier holds.
```

Tools: `playtest_run` (everything in one call, returns the Markdown report), `playtest_open`, `playtest_screenshot`, `playtest_press`, `playtest_click`, `playtest_drag`, `playtest_type`, `playtest_wait`, `playtest_ui` (controls with coordinates, panels with rects), `playtest_measure` (why is this panel tiny), `playtest_state`, `playtest_console`, `playtest_smoke`, `playtest_discover`, `playtest_ui_stress`, `playtest_report`, `playtest_close`. With the primitives, the coding agent itself can play the game: read the UI, press keys, watch what changed, and write its own findings.

## Hints, optional

A `playtest.yaml` next to the game narrows the probe without replacing it:

```yaml
start_button: Open the Vault
keys: [b, r, l, m, escape, space]
build_cmd: npm install && npx vite build
serve_dir: dist
```

## Output

`runs/<timestamp>/report.md` for the agent, `report.json` for tooling, `report.html` for people, screenshots for evidence, and the rrweb replay link of the whole session.

## Requirements and limits

Python 3.11+. WebGL on Solari is software rendered (Mesa llvmpipe), so frame rates are lower than on a GPU; the report says so. Canvas-only interactions (clicking things drawn in WebGL) are not discovered by the deterministic pass; the MCP primitives let the coding agent do those by hand, and the autonomous runner (browser-use on `SolariBrowser`) is coming next.

## License

MIT.
