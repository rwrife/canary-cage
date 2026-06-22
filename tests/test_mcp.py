"""Tests for the MCP server mode."""

from __future__ import annotations

import io
import json
from pathlib import Path

from canary_cage import __version__
from canary_cage.canaries import MarkdownCanary
from canary_cage.config import PlantFilter, load_config
from canary_cage.mcp import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    TOOLS,
    attestation_path,
    handle_request,
    load_attestations,
    save_attestation,
    serve,
)
from canary_cage.state import load_state, save_state


def _seed_repo(root: Path) -> None:
    (root / "README.md").write_text("# Hello\n\nA quick intro.\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text(
        "# Guide\n\nSome words.\n", encoding="utf-8"
    )
    state = load_state(root)
    config = load_config(root)
    planted = MarkdownCanary().plant(root, PlantFilter(config))
    state.canaries.extend(planted)
    save_state(root, state)


def _call(root: Path, method: str, params: dict | None = None, req_id: int = 1):
    request = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    return handle_request(root, request)


def _call_tool(root: Path, name: str, args: dict | None = None, req_id: int = 1):
    return _call(
        root,
        "tools/call",
        {"name": name, "arguments": args or {}},
        req_id,
    )


def test_initialize_advertises_tools_capability(tmp_path: Path) -> None:
    res = _call(tmp_path, "initialize")
    assert res["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert res["result"]["serverInfo"] == {
        "name": SERVER_NAME,
        "version": __version__,
    }
    assert "tools" in res["result"]["capabilities"]


def test_initialized_notification_is_silent(tmp_path: Path) -> None:
    # Notifications have no id; per JSON-RPC, no response should be sent.
    assert (
        handle_request(
            tmp_path, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )


def test_tools_list_returns_three_tools(tmp_path: Path) -> None:
    res = _call(tmp_path, "tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert names == {"canary_list", "canary_status", "canary_attest"}
    for t in res["result"]["tools"]:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_unknown_method_returns_method_not_found(tmp_path: Path) -> None:
    res = _call(tmp_path, "nope/nada")
    assert res["error"]["code"] == -32601


def test_canary_list_empty(tmp_path: Path) -> None:
    res = _call_tool(tmp_path, "canary_list")
    payload = res["result"]
    assert payload["isError"] is False
    assert payload["structuredContent"] == {"count": 0, "canaries": []}


def test_canary_list_returns_planted_canaries(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    res = _call_tool(tmp_path, "canary_list")
    data = res["result"]["structuredContent"]
    assert data["count"] >= 1
    first = data["canaries"][0]
    assert {"id", "type", "path", "marker_preview", "planted_at"} <= set(first)
    assert first["type"] == "markdown"


def test_canary_status_summary(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    res = _call_tool(tmp_path, "canary_status")
    data = res["result"]["structuredContent"]
    assert data["server"] == SERVER_NAME
    assert data["version"] == __version__
    assert data["total"] >= 1
    assert "markdown" in data["by_type"]
    assert data["attestations"] == 0
    assert data["last_attestation"] is None


def test_canary_status_reports_last_fire(tmp_path: Path) -> None:
    fired = tmp_path / ".canary-cage" / "fired"
    fired.mkdir(parents=True)
    (fired / "abc123.json").write_text("{}", encoding="utf-8")
    res = _call_tool(tmp_path, "canary_status")
    last_fire = res["result"]["structuredContent"]["last_fire"]
    assert last_fire is not None
    assert last_fire["file"] == "abc123.json"


def test_canary_attest_records_and_dedupes(tmp_path: Path) -> None:
    first = _call_tool(
        tmp_path,
        "canary_attest",
        {"agent": "claude-code", "purpose": "refactor"},
    )
    assert first["result"]["isError"] is False
    assert first["result"]["structuredContent"]["ok"] is True

    # Calling again with same agent name should refresh, not duplicate.
    _call_tool(tmp_path, "canary_attest", {"agent": "claude-code"})
    records = load_attestations(tmp_path)
    assert len(records) == 1
    assert records[0]["agent"] == "claude-code"
    assert attestation_path(tmp_path).exists()


def test_canary_attest_requires_agent(tmp_path: Path) -> None:
    res = _call_tool(tmp_path, "canary_attest", {})
    assert res["result"]["isError"] is True
    assert "agent" in res["result"]["content"][0]["text"].lower()


def test_canary_attest_status_reflects_attestation(tmp_path: Path) -> None:
    _call_tool(tmp_path, "canary_attest", {"agent": "gpt-coder"})
    res = _call_tool(tmp_path, "canary_status")
    data = res["result"]["structuredContent"]
    assert data["attestations"] == 1
    assert data["last_attestation"]["agent"] == "gpt-coder"


def test_tools_call_unknown_tool_is_error(tmp_path: Path) -> None:
    res = _call_tool(tmp_path, "no_such_tool")
    assert res["result"]["isError"] is True


def test_serve_handles_stdio_loop(tmp_path: Path) -> None:
    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "",  # blank line should be ignored
        "{not json}",  # malformed should be ignored
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "canary_status", "arguments": {}},
            }
        ),
    ]
    stdin = iter(requests)
    stdout = io.StringIO()
    serve(tmp_path, stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
    # initialize + tools/list + tools/call = 3 responses; notification skipped.
    assert [r["id"] for r in lines] == [1, 2, 3]
    assert lines[1]["result"]["tools"]


def test_tool_schemas_disallow_extras() -> None:
    for spec in TOOLS.values():
        schema = spec["inputSchema"]
        assert schema["additionalProperties"] is False


def test_save_attestation_writes_relative_file(tmp_path: Path) -> None:
    save_attestation(tmp_path, agent="agent-a")
    save_attestation(tmp_path, agent="agent-b", purpose="ci")
    records = load_attestations(tmp_path)
    assert {r["agent"] for r in records} == {"agent-a", "agent-b"}
