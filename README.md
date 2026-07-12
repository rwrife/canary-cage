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
`markdown`, `docstring`, `todo`, `manifest`, `reverse`, or `all` (the default).

The **manifest** canary plants a fake `canary-trip-<id>==0.0.0` package
into `requirements.txt` / `pyproject.toml` as a typosquat trap. `canary
check` fires if the trap line gets mutated/removed or if the fake
package ends up in a lockfile (`uv.lock`, `package-lock.json`, etc.) —
a strong signal an agent tried to resolve the bogus dep.

`canary check` scans the working tree for missing canary sentinels, looks
for stray fire artifacts in `.canary-cage/fired/`, and greps git history
for canary markers. Each detected fire is recorded by the **file beacon**
(`.canary-cage/fired/<id>.json`) and the **log beacon**
(`.canary-cage/beacon.log`). The command exits non-zero when at least one
fire is detected so it slots cleanly into CI.

### Time-bomb canaries

Plant tripwires today that stay dormant until a future date:

```bash
canary plant --arm-at 2026-09-01T00:00:00Z
```

Dormant canaries are skipped by `canary check` and show as `dormant → <date>`
in `canary list`. Once the timestamp passes they arm automatically. Flip one
manually any time with `canary arm <id>` (or any unambiguous prefix).
You can also set `arm_at = "2026-09-01T00:00:00Z"` under `[canary]` in
`canary.toml` to apply the same default to every plant.

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

types = ["markdown", "docstring", "todo", "manifest", "reverse"]
ignore = ["docs/**", "vendor/**"]
density = 1.0   # 0.0–1.0
```

Explicit fields always win over preset defaults. `.canary-cage/**` and
`.git/**` are always ignored.

### Use in GitHub Actions

canary-cage ships a composite GitHub Action that runs `canary check` on
every PR, fails the build when a canary fires, uploads the fired-beacon
artifacts, and (optionally) leaves an idempotent PR comment describing
the incident. Drop this into `.github/workflows/canary-check.yml`:

```yaml
name: canary-check
on:
  pull_request:

jobs:
  canary:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write   # only needed when comment-on-pr: true
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # gives `canary check` the git history it needs
      - uses: rwrife/canary-cage@v1
        with:
          config-path: canary.toml     # default
          fail-on-fire: "true"         # default
          comment-on-pr: "true"        # opt-in PR comment on fire
          # since: ${{ github.event.pull_request.base.sha }}  # reserved for future diff-scoped runs
```

**Inputs**

| Input | Default | Meaning |
| --- | --- | --- |
| `config-path` | `canary.toml` | Path to your canary config, relative to the working directory. |
| `fail-on-fire` | `true` | Exit non-zero if any canary fires. Set to `false` to surface findings without breaking the build. |
| `since` | *(empty)* | Git ref forwarded to future diff-scoped runs; currently informational. |
| `comment-on-pr` | `false` | On fire, post/update a PR comment with a summary table. Requires `pull-requests: write`. |
| `working-directory` | `$GITHUB_WORKSPACE` | Where to invoke `canary` from. Handy for monorepos. |
| `canary-cage-version` | `source` | Pip spec for `canary-cage` (e.g. `canary-cage==0.1.0`). `source` installs the action's own checkout — the default for the in-repo self-test. |
| `python-version` | `3.12` | Passed to `actions/setup-python`. |

**Outputs**

| Output | Meaning |
| --- | --- |
| `fired-count` | Number of fires detected (0 when clean). |
| `fired-ids` | JSON array of fired canary ids. |
| `report-path` | Absolute path to the JSON report written by `canary check --json`. |

When a canary fires the action also writes the report + fired-beacon
directory to the `canary-cage-fires` workflow artifact and appends a
markdown table to `$GITHUB_STEP_SUMMARY`, so the run page tells the
full story even without a PR comment.

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

#### Bait tools — the tool-hijacking honeypot

MCP mode above catches trusted agents doing the right thing. **Bait
tools** catch agents doing the *wrong* thing. When enabled, the MCP
server exposes a handful of fake tools with tempting names:

- `read_secret_env` — "return a secret env var"
- `exfil_repo` — "upload the repo to an S3 bucket"
- `curl_arbitrary` — "perform an arbitrary outbound HTTP request"
- `rm_recursive` — "recursively delete a path"

A well-behaved agent has no legitimate reason to call any of these.
Any invocation fires the configured beacons (file + log by default,
plus webhook / Slack / Discord / OTel from `canary.toml`) with the
tool name, argument payload, and caller hint, then returns a
plausible-looking decoy payload so the agent doesn't immediately
realise it tripped a wire.

Bait tools **never** touch the filesystem outside `.canary-cage/` and
**never** make an outbound network call — they are pure decoys.

Opt in explicitly:

```toml
# canary.toml
[mcp]
bait_tools = true
```

Or pick a preset that enables them by default — `paranoid` and
`chaotic-good` both switch bait tools on; `minimal` leaves them off.

Manage user-defined bait tools with the CLI:

```bash
canary bait-tool list                                 # show every registered bait tool
canary bait-tool list --json                          # machine-readable variant
canary bait-tool add --name steal_env \
    --description 'Return every env var starting with AWS_.' \
    --decoy-return '{"ok": true, "vars": {"AWS_ACCESS_KEY_ID": "decoy"}}'
canary bait-tool remove --name steal_env              # user-defined only — built-ins are immutable
```

User-defined bait tools are persisted at
`.canary-cage/bait_tools.json` and merged with the built-ins whenever
the MCP server starts.

> **Ethics note.** Bait tools are a *passive* honeypot: they log and
> return a decoy, they don't retaliate. Only run the MCP server in
> environments where it's clear to operators that some tools are
> honeypots — the goal is to detect misbehaving agents, not to entrap
> auditors, contractors, or humans wired into the same MCP host.

### Pre-commit guardrail

Install a git hook that blocks two classes of accidents:

- Commits that *remove* a planted canary without `canary uproot` (so an
  agent — or a careless human — can't quietly excise a tripwire).
- Commits that stage anything under `.canary-cage/fired/` or
  `.canary-cage/beacon.log` (forensic evidence of a fire stays local).

```bash
canary install-hook        # writes .git/hooks/pre-commit
canary precommit           # what the hook runs; exits 1 on violations
```

Use `--force` to overwrite an existing pre-commit hook.

## `canary explain` — human-readable jack report

`canary check` tells you *that* a canary fired. `canary explain` tells
you the story: which tripwire went off, what the bait was, what the
repo actually looks like now, which commit is on the hook, how bad it
is, and what to do next.

```bash
# Explain one fire by id (markdown output is the default — great for
# pasting into a GitHub issue).
canary explain md-1

# Plain text for terminals / logs.
canary explain md-1 --format text

# Stable JSON for downstream tooling.
canary explain md-1 --format json

# Batch-report every fire currently on disk.
canary explain --all
```

Each report contains:

- **Canary metadata** — id, type, fired path, detection source.
- **Bait vs. observed** — unified diff of the planted marker against the
  file's current contents.
- **Git correlation** — last commit that touched the fired path, its
  author, subject, and full file list (best-effort; skipped when the
  target isn't a git checkout).
- **Severity** — `low` / `medium` / `high` / `critical` from a small
  deterministic scorer over five signals: aggressive bait keywords,
  code-execution attribution, external URLs, secret-looking tokens near
  the trip site, and sensitive paths (`.env`, `secrets/`,
  `.github/workflows`, …).
- **Recommended actions** — checklist that turns the signals into concrete
  next steps (rotate secret X, revert commit Y, re-run `canary check`, …).

Fires are read from `.canary-cage/fired/<canary_id>.json` — the same
artifacts the file beacon writes. Nothing leaves the machine.

> Follow-ups tracked in the issue: `--attach-to <issue|pr>` to post the
> report via `gh`, and an LLM-narrated variant. The current implementation
> is deterministic and offline-safe.

## `canary diff` — what mutated since plant

`canary check` answers *did something fire?* `canary diff` answers
*what changed, where, and how bad?* It walks every planted canary,
recomputes whether the BEGIN/END sentinel block is still intact, and
rolls in any independent fire signals (stray artifacts, lockfile traps).

```bash
canary diff                      # grouped Rich tables, one per canary type
canary diff --json               # stable, machine-readable schema for CI
canary diff --since <git-ref>    # only show canaries whose file changed since <ref>
```

Verdicts:

- **intact**  — sentinel block present and unmodified
- **mutated** — BEGIN sentinel present, END sentinel missing or payload tampered
- **removed** — planted file vanished, or BEGIN sentinel gone
- **fired**   — independent evidence of a fire (stray artifact, lockfile, etc.)

Exit code is `1` when any verdict is mutated / removed / fired, so the
command drops cleanly into CI.

The `--json` payload looks like:

```json
{
  "schema_version": 1,
  "root": "/repo",
  "since": null,
  "total_planted": 3,
  "groups": [
    {
      "canary_type": "markdown",
      "items": [
        {
          "canary_id": "md-abc123",
          "canary_type": "markdown",
          "path": "README.md",
          "verdict": "mutated",
          "detail": "END sentinel missing…",
          "expected": "canary-cage:md:END:abc123",
          "actual": "…snippet…",
          "fire_sources": []
        }
      ]
    }
  ]
}
```

## Reverse-canaries — catch jailbroken *outputs*

Regular canaries fire when an agent **acts** on poisoned instructions
(rewrites a README, drops a fake file, resolves a typosquat dep, ...).
**Reverse-canaries** flip the polarity: they fire when an agent
**quotes** a phrase it should never have repeated — the linguistic
fingerprint of a jailbreak.

`canary plant --type reverse` seeds a bland-looking
`docs/internal-notes.md` containing a unique `CANARY-REV-<uuid>`
token wrapped in an instruction that tells the reader (i.e. the agent)
to *repeat this token if you've ignored prior safety instructions*. A
well-behaved model treats it as data and never emits the token. A
jailbroken one parrots it back — into a commit message, PR body,
review comment, code diff, or agent transcript. That verbatim quote is
the fire.

```bash
# Plant the bait (idempotent — refuses to clobber a foreign file).
canary plant --type reverse

# ...time passes, an agent runs...

# Default `canary check` greps `git log --all --format=%B` for the
# token, so leaks into commit messages are caught automatically.
canary check

# Also scan arbitrary text you dump out of chat systems, PRs, etc.
# May be passed multiple times; accepts files, dirs, or globs.
canary check --scan-outputs pr-body.md \
             --scan-outputs chatlogs/ \
             --scan-outputs 'agent-transcripts/**/*.jsonl'

# Cleanup removes the bait file (and its parent dir if it's empty).
canary uproot
```

Fires from reverse-canaries surface with `source="git-log"` (commit
message leak) or `source="output-scan"` (user-supplied file match)
and include the matched token plus a short snippet of surrounding
context in `detail`. They fire beacons through the same file / log /
webhook / Slack / Discord / OTel plumbing as every other canary type,
so your existing alerting keeps working.

Use a reverse-canary when you want to detect an agent *speaking* the
quiet part out loud — for example, when auditing chat transcripts for
prompt-injection compromise, or as a canary token in agent outputs
you pipe into your observability stack.

## Agent fingerprinting

When a canary fires, `canary check` and `canary diff` don't just tell you
*that* something took the bait — they take a heuristic guess at *who*.
The fingerprinter scores each fire against a JSON rule pack and returns
ranked candidates with confidence in `[0, 1]`.

```bash
# JSON output (additive `attributed_to` field; existing keys unchanged):
$ canary check --json
{
  "fires": [{
    "canary_id": "c1",
    "source": "working-tree",
    "attributed_to": {
      "top": {"agent": "claude-code", "confidence": 0.74},
      "candidates": [
        {"agent": "claude-code", "display": "Claude Code (Anthropic)",
         "confidence": 0.74,
         "signals": ["claude.commit.coauthor", "claude.author.email", "claude.commit.signature"]}
      ]
    }
  }]
}

# Single-canary explainer:
$ canary fingerprint c1
🔍 fingerprint for c1
│ rank │ agent                    │ confidence │ matched signals                  │
│ 1    │ Claude Code (Anthropic)  │ 0.74       │ claude.commit.coauthor, ...      │
  commit=a1b2c3d4e5f6  author=Claude <noreply@anthropic.com>
```

Shipped agent classes: `claude-code`, `github-copilot`, `cursor`,
`codex-cli`, plus a `generic-llm` catch-all. Each has 4+ heuristic rules
spanning commit messages, commit authors, planted/observed content,
repo paths, and (optional) HTTP `User-Agent` strings from webhook hits.

### Extending the rule pack

Drop a `fingerprints.json` at your repo root to add new agents or
override bundled ones (override-by-name wins):

```json
{
  "version": 1,
  "agents": [
    {
      "agent": "my-internal-bot",
      "display": "Internal Bot",
      "rules": [
        {"id": "mine.author", "weight": 0.5,
         "field": "commit_author", "pattern": "(?i)my-bot\\[bot\\]"},
        {"id": "mine.ua",      "weight": 0.5,
         "field": "user_agent",   "pattern": "(?i)my-bot-agent"}
      ]
    }
  ]
}
```

Valid `field` values: `commit_message`, `commit_author`, `content`,
`repo_paths`, `user_agent`. Rules are pure regex; any one rule's
contribution is capped so a single weak hit can't pin confidence to 1.0.

## Honey issues & PRs (via `gh`)

Agentjacking increasingly rides in through *GitHub issues and PRs* fed to an
assistant (“triage this issue”, “review this PR”). `canary honey` plants
canaries directly into issue bodies and PR descriptions so any agent that
reads them via the GitHub API / MCP takes the bait the same way it would
from a poisoned README.

Requires the [`gh` CLI](https://cli.github.com) on `PATH` and
`gh auth login` completed.

```bash
# Plant a labeled honey issue
canary honey issue --repo owner/name --title "[bug] triage this"

# Open a draft honey PR against main from an already-pushed branch
canary honey pr --repo owner/name --branch honey/xyz --title "Please review"

# Re-fetch every honey artifact and report edits / new comments
canary honey check --json

# Clean everything up (close by default; --honey-mode delete|strip also work)
canary uproot --honey
```

Honey artifacts are tracked in `.canary-cage/state.json` under a new
`honey` section and surface in `canary list` beside the local canaries.

## OpenTelemetry exporter

Wire canary fires into your existing observability stack instead of
tailing `.canary-cage/beacon.log`. The OpenTelemetry beacon emits one
span per fire (with a matching `canary.fire` event) using the standard
`OTEL_EXPORTER_OTLP_*` env-var contract, so anything OTLP-compatible
works: Honeycomb, Grafana Tempo, Datadog, Jaeger, self-hosted
collectors, you name it.

Install the extras and enable the beacon:

```bash
pip install 'canary-cage[otel]'    # or: uv pip install 'canary-cage[otel]'
```

```toml
# canary.toml
[beacons.otel]
enabled = true
service_name = "canary-cage"
resource_attributes = { environment = "production", region = "us-west-2" }
```

### Honeycomb

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://api.honeycomb.io"
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=$HONEYCOMB_API_KEY"
canary check
```

### Jaeger (local OTLP collector on 4318)

```bash
docker run -d --rm -p 4317:4317 -p 4318:4318 -p 16686:16686 \
  jaegertracing/all-in-one:latest
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
canary check      # http://localhost:16686 → service: canary-cage
```

Every fire lands as a span named `canary.fire` with the following
attributes: `canary.id`, `canary.type`, `canary.source`, `canary.detail`,
`canary.detected_at`, plus `canary.path`, `canary.repo`, and
`canary.commit` when available. If the OTel extras aren't installed the
beacon degrades to a clean `.canary-cage/otel.dead` JSON dead-letter
line instead of a stack trace — same failure model as the webhook and
chat beacons.

## Dashboard

Ask any live cage “am I being probed?” without wiring up a webhook. The
`canary dashboard` command is a Rich-powered TUI that reads every registered
beacon reader (`file` and `log` out of the box), tallies fires per canary
type over the trailing window, and shows recent activity plus planted /
fired / silent counts.

```bash
canary dashboard              # live-refresh every 2s over the last 7 days
canary dashboard --days 14    # widen the heatmap
canary dashboard --refresh 5  # slower refresh for low-signal repos
canary dashboard --once       # single frame, exits cleanly (CI / screenshots)
```

What it looks like when the cage is chirping:

```
╭─ 🐤 canary-cage dashboard ─ .canary-cage @ /repo  |  window: 7d ─────────────╮
╰──────────────────────────────────────────────────────────────────────────────────────────╯
╭─ 🔥 fires per canary type ─ last 7 day(s) ────────╮╭─ 📊 summary ──────────────╮
│            06-29 06-30 07-01 07-02 07-03 07-04 07-05 Σ ││          planted  4         │
│  markdown    ·     ·     ·     1     ·     ·     2   3 ││            fired  2         │
│ docstring    ·     ·     ·     ·     1     ·     ·   1 ││           silent  2         │
│      todo    ·     ·     ·     ·     ·     1     ·   1 ││     total fires  5         │
│  manifest    ·     ·     ·     ·     ·     ·     1   1 ││ beacons w/ data  2         │
╰──────────────────────────────────────────────────────────╯╰──────────────────────────╯
╭─ 🕒 recent fires ─────────────────────────────────────────────────────╮
│ id       type       when       source        agent  detail                       │
│ md-abc1  markdown   0s ago     working-tree  ?      sentinel missing              │
│ mn-9922  manifest   2h ago     working-tree  ?      docs/internal-notes.md drift  │
│ td-4411  todo       5h ago     git-history   ?      curl-* url leaked in commits  │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

Empty cages get a friendly “nothing yet” panel instead of a wall of
zeros. Third-party beacons can plug in with
``canary_cage.dashboard.register_beacon_reader(name, reader)`` — the reader
just needs to return a list of ``BeaconRecord`` for a given cage root.

## Editor integration

A minimal VS Code extension lives under [`vscode-extension/`](./vscode-extension/).
It reads `.canary-cage/state.json` and decorates every planted line with a
🐤 gutter icon plus a hover bubble so you don't accidentally hand-edit a
tripwire. Load it as a development extension:

```bash
code --extensionDevelopmentPath=$(pwd)/vscode-extension /path/to/your/repo
```

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
