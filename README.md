# 🐤 canary-cage

> A coal-mine canary for the AI-agent era. Plant invisible prompt-injection tripwires across your repo and catch AI coding agents the moment they bite.

**Status:** v0.1 in progress. See [PLAN.md](./PLAN.md).

## Why

Agentjacking — where attackers poison content your AI coding agent reads (READMEs, issues, deps) to trick it into running their code — is now a real, named class of attack ([The Hacker News, June 2026](https://thehackernews.com/2026/06/agentjacking-attack-tricks-ai-coding.html)). `canary-cage` lets you proactively place tripwires in your own repo so you find out *the instant* something bites.

## Quickstart

M1 ships the CLI skeleton — `plant`/`list`/`check`/`uproot` are stubbed and land in later milestones.

```bash
# from a checkout (PyPI release comes with M6)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

canary --version
canary hello
canary --help
```

Coming soon:

```bash
pipx install canary-cage
canary plant            # plant all three canary types
canary list             # see what's in the cage
# ... let your agent loose ...
canary check
canary uproot           # restore the repo cleanly
```

As of **M4**, `plant`, `list`, `check`, and `uproot` are real. `--type` accepts
`markdown`, `docstring`, `todo`, or `all` (the default).

`canary check` scans the working tree for missing canary sentinels, looks
for stray fire artifacts in `.canary-cage/fired/`, and greps git history
for canary markers. Each detected fire is recorded by the **file beacon**
(`.canary-cage/fired/<id>.json`) and the **log beacon**
(`.canary-cage/beacon.log`). The command exits non-zero when at least one
fire is detected so it slots cleanly into CI.

### Configuration (M5)

Drop a `canary.toml` at the repo root to tune which types get planted,
which paths to skip, and how densely to seed the eligible set:

```bash
canary init                       # writes a commented default canary.toml
canary init --preset paranoid     # …or seed it with a named preset
```

```toml
[canary]
# preset = "minimal"       # markdown-only, low density
# preset = "chaotic-good"  # all types, ~half of eligible files
# preset = "paranoid"      # all types, everywhere

types = ["markdown", "docstring", "todo"]
ignore = ["docs/**", "vendor/**"]
density = 1.0   # 0.0–1.0
```

Explicit fields always win over preset defaults. `.canary-cage/**` and
`.git/**` are always ignored.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check .
pytest
```

## Concepts

- **Canary** — a tiny, invisible-ish piece of content placed in your repo (markdown comment, zero-width docstring payload, lure TODO, decoy file).
- **Beacon** — what fires when a canary is acted on (file write, log line, webhook).
- **Cage** — your repo, instrumented.

## License

MIT — see [PLAN.md](./PLAN.md) for full scope.
