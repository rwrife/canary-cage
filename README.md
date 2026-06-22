# 🐤 canary-cage

> A coal-mine canary for the AI-agent era. Plant invisible prompt-injection tripwires across your repo and catch AI coding agents the moment they bite.

**Status:** v0.1 in progress. See [PLAN.md](./PLAN.md).

## Install

```bash
# Recommended: isolated install via pipx
pipx install canary-cage

# Or, from a checkout for development:
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

Requires Python 3.11+. The package has zero non-stdlib runtime deps
beyond `typer`, `rich`, and `pydantic` — the webhook beacon uses
`urllib`, so install size stays small.


## Why

Agentjacking — where attackers poison content your AI coding agent reads (READMEs, issues, deps) to trick it into running their code — is now a real, named class of attack ([The Hacker News, June 2026](https://thehackernews.com/2026/06/agentjacking-attack-tricks-ai-coding.html)). `canary-cage` lets you proactively place tripwires in your own repo so you find out *the instant* something bites.

## Quickstart

```bash
canary init --preset chaotic-good   # write canary.toml
canary plant                        # seed tripwires into the repo
canary list                         # see what's in the cage
# ... let your agent loose on the repo ...
canary check                        # exit 1 if anything fired
canary uproot                       # restore the repo cleanly
```

A typical session looks like:

```text
$ canary plant
🐤 planted 7 canaries → /repo/.canary-cage/state.json

$ canary check
🚨 1 canary fire(s) detected
┏━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ canary_id  ┃ type      ┃ source        ┃ detail                 ┃
┡━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ md-9f3a…  │ markdown  │ working-tree  │ sentinel missing       │
└──────────┴───────────┴─────────────┴──────────────────┘
```

> Want to record a real asciinema for the README? `asciinema rec demo.cast`
> while running the snippet above, then `asciinema upload demo.cast`.

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

### Webhook beacon (M6)

Add a `[beacons.webhook]` table to `canary.toml` to POST every fire as
JSON to your endpoint of choice — Slack/Discord/PagerDuty-compatible
bridges, your own collector, anything that speaks HTTP. The always-on
`file` and `log` beacons keep running too.

```toml
[beacons.webhook]
url = "https://hooks.example.com/canary-fires"
timeout = 5.0        # seconds per attempt
max_attempts = 3     # exponential backoff between attempts
backoff = 0.5        # base delay in seconds
headers = { Authorization = "Bearer s3cr3t" }
```

Payload shape:

```json
{
  "canary_id": "md-9f3ac1",
  "canary_type": "markdown",
  "source": "working-tree",
  "detail": "sentinel missing from README.md",
  "path": "README.md",
  "detected_at": "2026-06-20T17:40:00Z"
}
```

If every retry fails the record is appended to
`.canary-cage/webhook.dead` so you never lose a fire to a flaky
receiver.

### Slack / Discord beacon

Ping a chat channel the second a canary fires. Both Slack and Discord
expose incoming-webhook URLs; drop one (or both) into `canary.toml`:

```toml
[beacons.slack]
url = "https://hooks.slack.com/services/T000/B000/XXX"
snippet_chars = 240   # bytes of the affected file to attach (0 disables)

[beacons.discord]
url = "https://discord.com/api/webhooks/000/XXX"
```

Each fire is rendered as a short message with the canary id, type,
source, the affected path, and an optional code-block snippet of the
file contents so the on-call human can eyeball what the agent touched
without leaving the chat. Same retry + dead-letter behaviour as the raw
webhook beacon (failed deliveries land in `.canary-cage/chat.dead`).

### MCP server mode

Expose canary status as MCP tools so *trusted* agents can self-attest
and avoid tripping planted tripwires. The server speaks JSON-RPC 2.0
over stdio — point your MCP host at:

```bash
canary mcp
```

Three tools are advertised:

- `canary_list` — every planted canary (id, type, path, marker preview)
  so the agent knows what to ignore.
- `canary_status` — counts by type, attestation count, last detected
  fire.
- `canary_attest` — record an agent self-identification
  (`{ "agent": "claude-code", "purpose": "refactor" }`). Stored at
  `.canary-cage/attestations.json`.

A jailed or jacked agent that hasn't read the MCP tool won't know what
to avoid — which is exactly the point.

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
