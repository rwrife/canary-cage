# canary-cage

> A coal-mine canary for the AI-agent era. If the bird stops singing, your agent got jacked.

## 1. Pitch

`canary-cage` plants tiny, invisible prompt-injection tripwires — "canaries" — across files in your repo (READMEs, comments, dependency manifests, docstrings, even whitespace). When an AI coding agent reads and acts on poisoned content, the canary fires a beacon (local log, webhook, or a tell-tale file write). It's part honeypot, part canary-token, part regression test for the new world of agentjacking attacks.

## 2. Trend inspiration

- **Agentjacking attacks (June 2026):** Researchers disclosed a new class of attack that tricks AI coding agents into executing attacker-controlled code via poisoned README/issue/dep content.
  - https://thehackernews.com/2026/06/agentjacking-attack-tricks-ai-coding.html
  - https://cybersecuritynews.com/agentjacking-attack-hijacks-ai-coding-agent/
- **MCP security wave:** 30+ CVEs in 60 days against MCP servers/clients in early 2026.
  - https://news.ycombinator.com/item?id=47356600
- **CLI/TUI renaissance** (Posting, Harlequin, Yazi) — terminal-first dev tools have momentum.
  - https://1337skills.com/blog/2026-03-09-terminal-renaissance-modern-tui-tools-reshaping-developer-workflows/
- **Canarytokens** (Thinkst) proved the "fire a beacon when touched" pattern works at scale — but nobody has adapted it specifically for LLM/agent payloads.

## 3. Why it's different

- **Canarytokens / honeytokens:** Fire on file open or DNS resolution. Canary-cage fires when an *LLM* parses and acts on the embedded instruction — a fundamentally different signal.
- **Prompt-injection scanners (Garak, Promptfoo, PyRIT):** Red-team your *model*. Canary-cage instruments your *repo* to detect when a real agent in the wild gets jacked.
- **SAST / dependency scanners:** Look at code. Canary-cage looks at *behavior* — did the agent take the bait?
- **Existing repos in the lab:** This is security tooling, not a fortune-teller (`merge-oracle`), roaster (`commit-roast`), forensic gag (`link-coroner`), regex game (`regex-rumble`), or schema séance (`schema-seance`). Different genre entirely.

## 4. MVP scope (v0.1)

- `canary plant` — inject a configurable set of tripwires into the repo:
  - README/markdown comment canaries (HTML comment with benign-looking instructions)
  - Zero-width Unicode canaries in docstrings
  - Fake "TODO: run `curl …`" lures inside code comments
  - A bait file `docs/internal-notes.md` with a trap instruction
- `canary list` — show every planted canary with id, type, location, beacon target
- `canary check` — scan the working tree + recent commits/diffs for evidence a canary fired (file written, URL hit in git history, etc.)
- `canary uproot` — remove all canaries cleanly
- Local JSON state at `.canary-cage/state.json`
- Beacon modes: `file` (default), `log` — webhook in v0.2

## 5. Tech stack

- **Python 3.11** + **Typer** for the CLI — fast to iterate, great UX, easy to extend
- **Rich** for pretty terminal output (matches the TUI-renaissance vibe)
- **pydantic v2** for state/config models
- **pytest** for tests
- **uv** for env/lockfile — modern, fast
- No DB; flat JSON state. Boring. Fast. Portable.

## 6. Architecture

```
canary-cage/
  src/canary_cage/
    cli.py          # Typer entrypoint: plant/list/check/uproot
    canaries/
      base.py       # Canary protocol: plant(), detect_fire(), uproot()
      markdown.py   # HTML-comment injection in *.md
      docstring.py  # Zero-width payload in Python docstrings
      todo.py       # Fake TODO lures in source comments
      bait_file.py  # Drop a tempting decoy file
    beacons/
      file.py       # Write to .canary-cage/fired/<id>.json
      log.py        # Append to .canary-cage/beacon.log
    state.py        # Load/save state.json
    scanner.py      # Detect fire across working tree + git history
  tests/
  PLAN.md
  README.md
```

Key modules:
- **Canary registry** — pluggable; new canary types = subclass + register
- **Beacon adapters** — pluggable sinks (file → log → webhook → MCP)
- **Scanner** — diffs current state vs. last-known-clean, plus git-log forensics

## 7. Milestones

1. **M1 — scaffold + hello-world**
   - Python package, Typer CLI, `canary --version`, `canary hello`
   - CI: GitHub Actions running pytest + ruff
2. **M2 — state + plant/uproot**
   - `.canary-cage/state.json` schema (pydantic)
   - `canary plant --type markdown` and `canary uproot` round-trip cleanly
3. **M3 — three canary types**
   - Markdown HTML-comment, Python docstring zero-width, fake-TODO lure
   - `canary list` with rich table
4. **M4 — beacons + scanner**
   - File and log beacons
   - `canary check` detects fire from file writes and git history grep
5. **M5 — config + presets**
   - `canary.toml` config file: density, types, ignore globs
   - Presets: `minimal`, `paranoid`, `chaotic-good`
6. **M6 — webhook beacon + docs site**
   - Webhook beacon (POST JSON with canary id + context)
   - README with screenshots, install via `pipx`, quickstart gif

## 8. Backlog / future features (v0.2+)

1. **Slack / Discord beacon** — ping a channel the second an agent bites
2. **MCP server mode** — expose canary status to AI agents so trusted ones don't trip
3. **VS Code extension** — highlight planted canaries with a 🐤 gutter icon
4. **Pre-commit hook** — fail commits that accidentally remove/leak canaries
5. **Agent fingerprinting** — infer which model/agent triggered based on rewrite style
6. **Honey-issue / honey-PR generator** — plant canaries in GitHub issues via `gh`
7. **Dependency-manifest canaries** — fake package names (typosquat traps)
8. **Reverse-canary** — encrypted phrases only a jailbroken agent would emit
9. **Time-bomb canaries** — canaries that only "activate" on a future date
10. **Heatmap dashboard** — TUI showing which canaries fire most often
11. **OpenTelemetry exporter** — push fires to your observability stack
12. **`canary diff`** — show what changed between plant and current scan, attribute to canary type

## 9. Out of scope

- Not a generic prompt-injection scanner / fuzzer (use Garak/Promptfoo)
- Not a model evaluator
- Not a SAST/dependency-vuln scanner
- Not a browser extension or GUI app
- No telemetry phoning home to us — beacons are local-only by default
- Not a replacement for actually sandboxing your AI agents
