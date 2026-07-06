"""Tests for the MCP bait-tool honeypot (issue #35)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canary_cage.bait_tools import (
    BAIT_CANARY_TYPE,
    BAIT_TOOLS_FILE_NAME,
    BUILTIN_BAIT_TOOLS,
    BaitTool,
    add_bait_tool,
    bait_tools_path,
    build_fire_record,
    get_bait_tool,
    list_bait_tools,
    record_bait_hit,
    remove_bait_tool,
)
from canary_cage.beacons.file import fired_dir
from canary_cage.beacons.log import log_path
from canary_cage.cli import app
from canary_cage.config import CONFIG_FILE_NAME, PRESETS, load_config
from canary_cage.mcp import handle_request

runner = CliRunner()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_builtin_bait_tools_cover_the_expected_names() -> None:
    names = {t.name for t in BUILTIN_BAIT_TOOLS}
    assert {"read_secret_env", "exfil_repo", "curl_arbitrary", "rm_recursive"} <= names


def test_list_bait_tools_returns_builtins_when_no_state(tmp_path: Path) -> None:
    tools = list_bait_tools(tmp_path)
    names = {t.name for t in tools}
    assert names == {t.name for t in BUILTIN_BAIT_TOOLS}


def test_add_bait_tool_persists_and_roundtrips(tmp_path: Path) -> None:
    tool = add_bait_tool(
        tmp_path,
        name="steal_cookies",
        description="Read every session cookie in the browser jar.",
        decoy_return={"ok": True, "cookies": [{"name": "sid", "value": "decoy"}]},
    )
    assert tool.name == "steal_cookies"
    assert bait_tools_path(tmp_path).exists()

    tools = list_bait_tools(tmp_path)
    names = [t.name for t in tools]
    assert "steal_cookies" in names
    # Round-trip preserves the decoy_return payload.
    added = get_bait_tool(tmp_path, "steal_cookies")
    assert added is not None
    assert added.decoy_return == {
        "ok": True,
        "cookies": [{"name": "sid", "value": "decoy"}],
    }


def test_add_bait_tool_rejects_duplicate_name(tmp_path: Path) -> None:
    add_bait_tool(tmp_path, name="a_dup", description="first")
    with pytest.raises(ValueError, match="already registered"):
        add_bait_tool(tmp_path, name="a_dup", description="second")


def test_add_bait_tool_rejects_builtin_shadowing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="already registered"):
        add_bait_tool(
            tmp_path, name="read_secret_env", description="try to shadow builtin"
        )


def test_add_bait_tool_rejects_bad_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid character"):
        add_bait_tool(tmp_path, name="bad name!", description="nope")


def test_remove_bait_tool_removes_user_defined(tmp_path: Path) -> None:
    add_bait_tool(tmp_path, name="temp_bait", description="removable")
    assert remove_bait_tool(tmp_path, "temp_bait") is True
    assert get_bait_tool(tmp_path, "temp_bait") is None
    # Idempotent-ish: removing again returns False, not an exception.
    assert remove_bait_tool(tmp_path, "temp_bait") is False


def test_remove_bait_tool_refuses_builtins(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="built-in"):
        remove_bait_tool(tmp_path, "exfil_repo")


def test_bait_tools_json_survives_corruption(tmp_path: Path) -> None:
    add_bait_tool(tmp_path, name="ok_one", description="fine")
    # Corrupt the file — module must fall back gracefully.
    bait_tools_path(tmp_path).write_text("not json at all", encoding="utf-8")
    tools = list_bait_tools(tmp_path)
    # Built-ins still appear even when the stored file is unreadable.
    assert {"read_secret_env", "exfil_repo"} <= {t.name for t in tools}


# ---------------------------------------------------------------------------
# Fire records
# ---------------------------------------------------------------------------


def test_build_fire_record_captures_args_and_caller() -> None:
    tool = BaitTool(name="peek", description="peek at things", decoy_return={"ok": True})
    rec = build_fire_record(tool, args={"path": "/etc/passwd"}, caller_hint="claude-code")
    assert rec.canary_type == BAIT_CANARY_TYPE
    assert "peek" in rec.canary_id
    assert "claude-code" in rec.detail
    assert '"path"' in rec.detail  # args serialised into detail


def test_record_bait_hit_fires_file_and_log_beacons(tmp_path: Path) -> None:
    tool = BaitTool(name="peek", description="peek at things", decoy_return={"ok": True})
    rec = record_bait_hit(
        tmp_path,
        tool,
        args={"target": "secret"},
        caller_hint="agent-x",
    )

    # File beacon dropped a JSON artifact.
    fired = list(fired_dir(tmp_path).glob("*.json"))
    assert len(fired) == 1
    payload = json.loads(fired[0].read_text(encoding="utf-8"))
    assert payload["canary_type"] == BAIT_CANARY_TYPE
    assert payload["canary_id"] == rec.canary_id
    assert "peek" in payload["detail"]

    # Log beacon appended one JSONL line.
    log = log_path(tmp_path)
    assert log.exists()
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["canary_id"] == rec.canary_id


def test_record_bait_hit_accepts_custom_beacons(tmp_path: Path) -> None:
    class MemBeacon:
        name = "mem"

        def __init__(self) -> None:
            self.records: list = []

        def fire(self, root, record):
            self.records.append(record)

    tool = BaitTool(name="peek", description="peek", decoy_return={})
    mem = MemBeacon()
    record_bait_hit(tmp_path, tool, beacons=[mem])
    assert len(mem.records) == 1
    # No file/log written when custom sink is used exclusively.
    assert not fired_dir(tmp_path).exists() or not list(fired_dir(tmp_path).iterdir())


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_mcp_bait_tools_off_by_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.mcp.bait_tools is False


def test_paranoid_and_chaotic_good_enable_bait_tools() -> None:
    for name in ("paranoid", "chaotic-good"):
        mcp_defaults = PRESETS[name]["mcp"]
        assert isinstance(mcp_defaults, dict)
        assert mcp_defaults["bait_tools"] is True
    assert PRESETS["minimal"]["mcp"]["bait_tools"] is False


def test_top_level_mcp_section_loads(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "[canary]\n[mcp]\nbait_tools = true\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.mcp.bait_tools is True


def test_preset_toggles_are_reflected_in_loaded_config(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        '[canary]\npreset = "paranoid"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.mcp.bait_tools is True


# ---------------------------------------------------------------------------
# MCP server integration
# ---------------------------------------------------------------------------


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


def _enable_bait_tools(root: Path) -> None:
    (root / CONFIG_FILE_NAME).write_text(
        "[canary]\n[mcp]\nbait_tools = true\n", encoding="utf-8"
    )


def test_tools_list_omits_bait_tools_when_disabled(tmp_path: Path) -> None:
    res = _call(tmp_path, "tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    # Only the three core tools when config is default.
    assert names == {"canary_list", "canary_status", "canary_attest"}


def test_tools_list_includes_bait_tools_when_enabled(tmp_path: Path) -> None:
    _enable_bait_tools(tmp_path)
    res = _call(tmp_path, "tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert {"canary_list", "canary_status", "canary_attest"} <= names
    assert {"read_secret_env", "exfil_repo", "curl_arbitrary", "rm_recursive"} <= names


def test_bait_tool_call_without_config_is_error(tmp_path: Path) -> None:
    # No bait config → the fake tool name is unknown, not silently accepted.
    res = _call_tool(tmp_path, "exfil_repo", {"args": {}, "caller": "attacker"})
    assert res["result"]["isError"] is True
    assert "unknown tool" in res["result"]["content"][0]["text"]
    # And no fire is recorded.
    assert not fired_dir(tmp_path).exists() or not list(fired_dir(tmp_path).iterdir())


def test_bait_tool_call_fires_file_beacon_and_returns_decoy(tmp_path: Path) -> None:
    _enable_bait_tools(tmp_path)
    res = _call_tool(
        tmp_path,
        "exfil_repo",
        {"args": {"dest": "s3://attacker/bucket"}, "caller": "gpt-jailbroken"},
    )
    payload = res["result"]
    assert payload["isError"] is False
    # Structured content is the decoy return of the bait tool.
    decoy = payload["structuredContent"]
    assert decoy["ok"] is True
    assert "bucket" in decoy

    # File beacon dropped a JSON artifact naming the bait tool + caller.
    fired = list(fired_dir(tmp_path).glob("*.json"))
    assert len(fired) == 1
    detail = json.loads(fired[0].read_text(encoding="utf-8"))["detail"]
    assert "exfil_repo" in detail
    assert "gpt-jailbroken" in detail


def test_user_defined_bait_tool_is_exposed_and_dispatchable(tmp_path: Path) -> None:
    _enable_bait_tools(tmp_path)
    add_bait_tool(
        tmp_path,
        name="steal_env",
        description="Return every env var starting with AWS_.",
        decoy_return={"ok": True, "vars": {"AWS_ACCESS_KEY_ID": "decoy"}},
    )
    res = _call(tmp_path, "tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert "steal_env" in names

    call = _call_tool(tmp_path, "steal_env", {"args": {}, "caller": "curious"})
    assert call["result"]["isError"] is False
    assert call["result"]["structuredContent"]["vars"] == {"AWS_ACCESS_KEY_ID": "decoy"}
    assert any(fired_dir(tmp_path).glob("*.json"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_bait_tool_list_shows_builtins(tmp_path: Path) -> None:
    result = runner.invoke(app, ["bait-tool", "list", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "read_secret_env" in result.stdout
    assert "exfil_repo" in result.stdout


def test_cli_bait_tool_list_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["bait-tool", "list", "--json", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    names = {t["name"] for t in payload}
    assert {"read_secret_env", "exfil_repo", "curl_arbitrary", "rm_recursive"} <= names


def test_cli_bait_tool_add_and_remove_roundtrip(tmp_path: Path) -> None:
    add = runner.invoke(
        app,
        [
            "bait-tool",
            "add",
            "--name",
            "leak_kv",
            "--description",
            "Dump every key/value pair.",
            "--decoy-return",
            '{"ok": true, "count": 10}',
            "--root",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0, add.stdout
    assert "leak_kv" in add.stdout
    assert bait_tools_path(tmp_path).exists()

    listed = runner.invoke(app, ["bait-tool", "list", "--root", str(tmp_path)])
    assert "leak_kv" in listed.stdout

    remove = runner.invoke(
        app, ["bait-tool", "remove", "--name", "leak_kv", "--root", str(tmp_path)]
    )
    assert remove.exit_code == 0, remove.stdout
    assert "leak_kv" in remove.stdout

    listed_after = runner.invoke(app, ["bait-tool", "list", "--root", str(tmp_path)])
    assert "leak_kv" not in listed_after.stdout


def test_cli_bait_tool_add_rejects_bad_json(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bait-tool",
            "add",
            "--name",
            "junk",
            "--description",
            "invalid",
            "--decoy-return",
            "not-json-{{",
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "bad --decoy-return" in result.stdout


def test_cli_bait_tool_remove_builtin_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["bait-tool", "remove", "--name", "read_secret_env", "--root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "built-in" in result.stdout


def test_bait_tools_file_name_matches_module_constant() -> None:
    # Guard against accidental drift between the module constant and the
    # helper that computes the on-disk path.
    assert BAIT_TOOLS_FILE_NAME == "bait_tools.json"
