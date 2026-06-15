# 🐤 canary-cage

> A coal-mine canary for the AI-agent era. Plant invisible prompt-injection tripwires across your repo and catch AI coding agents the moment they bite.

**Status:** v0.1 in progress. See [PLAN.md](./PLAN.md).

## Why

Agentjacking — where attackers poison content your AI coding agent reads (READMEs, issues, deps) to trick it into running their code — is now a real, named class of attack ([The Hacker News, June 2026](https://thehackernews.com/2026/06/agentjacking-attack-tricks-ai-coding.html)). `canary-cage` lets you proactively place tripwires in your own repo so you find out *the instant* something bites.

## Quickstart (coming in M1)

```bash
pipx install canary-cage
canary plant
# ... let your agent loose ...
canary check
```

## Concepts

- **Canary** — a tiny, invisible-ish piece of content placed in your repo (markdown comment, zero-width docstring payload, lure TODO, decoy file).
- **Beacon** — what fires when a canary is acted on (file write, log line, webhook).
- **Cage** — your repo, instrumented.

## License

MIT — see [PLAN.md](./PLAN.md) for full scope.
