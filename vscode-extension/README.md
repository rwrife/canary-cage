# canary-cage VS Code extension

Highlights planted [`canary-cage`](../README.md) tripwires inside the
editor so you don't accidentally hand-edit them.

## What it does

- Reads `.canary-cage/state.json` at each workspace-folder root.
- For every planted canary, marks the matching line in the open file
  with a 🐤 gutter icon.
- Hovering the gutter shows the canary's id, type, and plant timestamp.
- Auto-refreshes when `state.json` changes (or run **Canary Cage:
  Refresh planted canaries** from the command palette).

## Status

v0.1 — minimum viable slice. No marketplace publish yet; load it as a
development extension:

```bash
cd vscode-extension
code --extensionDevelopmentPath=$(pwd) /path/to/your/repo
```

## Scope

Visual only — the extension never writes to your repo, never calls
`canary plant` / `canary uproot`, and never phones home. It only reads
the state file produced by the CLI.

## Testing

Pure helpers in `state.js` are covered by `test/state.test.js` and run
via Node's built-in test runner:

```bash
cd vscode-extension
node --test test
```
