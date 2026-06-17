"""Smoke tests for the Typer CLI."""

from __future__ import annotations

from typer.testing import CliRunner

from canary_cage import __version__
from canary_cage.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_hello_default() -> None:
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "hello, world" in result.stdout


def test_hello_named() -> None:
    result = runner.invoke(app, ["hello", "ryan"])
    assert result.exit_code == 0
    assert "hello, ryan" in result.stdout


def test_help_lists_planned_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("hello", "plant", "list", "check", "uproot"):
        assert cmd in result.stdout


def test_list_placeholder_exits_nonzero() -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 2
