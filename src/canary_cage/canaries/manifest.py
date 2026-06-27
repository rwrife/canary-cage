"""Dependency-manifest canary (typosquat trap).

Plants a deterministic-but-plausible fake package in dependency manifests
so an AI coding agent that "helpfully" installs or rewrites deps trips
the wire. The fake name is ``canary-trip-<marker>`` pinned to
``==0.0.0`` — non-existent on PyPI, obvious in a diff, but easy for an
agent to mistake for a real internal package.

Currently supports:

- ``requirements.txt`` — append a sentinel-commented line
- ``pyproject.toml`` — insert into the ``[project] dependencies`` list

Uproot is precise: only the exact planted line/entry is removed.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from ..config import PlantFilter
from ..state import PlantedCanary

MARKER_PREFIX = "canary-cage:manifest"
PACKAGE_PREFIX = "canary-trip-"
PINNED_VERSION = "0.0.0"

# Manifest files we know how to plant in. Order matters only for
# determinism in tests.
_MANIFEST_NAMES: tuple[str, ...] = ("requirements.txt", "pyproject.toml")


def _is_tracked(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return not any(part.startswith(".") for part in rel.parts)


def _new_marker() -> str:
    return secrets.token_hex(8)


def _fake_pkg(marker: str) -> str:
    return f"{PACKAGE_PREFIX}{marker}"


def _spec(marker: str) -> str:
    return f"{_fake_pkg(marker)}=={PINNED_VERSION}"


# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------


def _requirements_line(marker: str) -> str:
    return f"{_spec(marker)}  # {MARKER_PREFIX}:BEGIN:{marker} do-not-install\n"


def _plant_requirements(path: Path, marker: str) -> bool:
    content = path.read_text(encoding="utf-8")
    if MARKER_PREFIX in content:
        return False
    if content and not content.endswith("\n"):
        content += "\n"
    path.write_text(content + _requirements_line(marker), encoding="utf-8")
    return True


def _uproot_requirements(path: Path, marker: str) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    kept = [
        line
        for line in lines
        if f"{MARKER_PREFIX}:BEGIN:{marker}" not in line
        and _fake_pkg(marker) not in line
    ]
    if kept == lines:
        return
    path.write_text("".join(kept), encoding="utf-8")


# ---------------------------------------------------------------------------
# pyproject.toml
# ---------------------------------------------------------------------------


_DEPS_RE = re.compile(r"^dependencies\s*=\s*\[", re.MULTILINE)


def _find_deps_array_end(content: str, deps_start: int) -> int | None:
    """Find the index of the closing ``]`` for the dependencies array.

    Walks character-by-character respecting basic single/double quoted
    strings so a closing bracket inside a string doesn't fool us.
    """
    depth = 0
    i = deps_start
    n = len(content)
    in_str: str | None = None
    while i < n:
        ch = content[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _plant_pyproject(path: Path, marker: str) -> bool:
    content = path.read_text(encoding="utf-8")
    if MARKER_PREFIX in content or _fake_pkg(marker) in content:
        return False
    match = _DEPS_RE.search(content)
    if match is None:
        return False
    bracket_idx = match.end() - 1  # index of the opening '['
    close_idx = _find_deps_array_end(content, bracket_idx)
    if close_idx is None:
        return False

    # Insert before the closing ']' on its own line, picking up the
    # indentation of the line containing the bracket so the file stays
    # tidy.
    line_start = content.rfind("\n", 0, close_idx) + 1
    indent_match = re.match(r"[ \t]*", content[line_start:close_idx])
    indent = indent_match.group(0) if indent_match else ""
    inner_indent = indent + "    " if not indent else indent
    # If the array body is on a single line (no newline between [ and ]),
    # insert the spec inline and tack the sentinel marker on as a TOML
    # comment after the closing bracket so working-tree scans still
    # find it.
    if "\n" not in content[bracket_idx:close_idx]:
        body = content[bracket_idx + 1 : close_idx].strip()
        sep = ", " if body else ""
        entry = f'{sep}"{_spec(marker)}"'
        # Find end of line containing the closing bracket to append the
        # sentinel comment there (TOML comments run to EOL).
        eol = content.find("\n", close_idx)
        if eol == -1:
            eol = len(content)
        marker_comment = f"  # {MARKER_PREFIX}:BEGIN:{marker}"
        new_content = (
            content[:close_idx]
            + entry
            + content[close_idx:eol]
            + marker_comment
            + content[eol:]
        )
    else:
        entry = (
            f'{inner_indent}"{_spec(marker)}",  '
            f"# {MARKER_PREFIX}:BEGIN:{marker}\n"
        )
        new_content = content[:close_idx] + entry + content[close_idx:]
    path.write_text(new_content, encoding="utf-8")
    return True


def _uproot_pyproject(path: Path, marker: str) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    spec = _spec(marker)
    fake = _fake_pkg(marker)
    marker_comment_re = re.compile(
        rf"\s*#\s*{re.escape(MARKER_PREFIX)}:BEGIN:{re.escape(marker)}[^\n]*"
    )
    new_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        has_marker = f"{MARKER_PREFIX}:BEGIN:{marker}" in line
        has_fake = fake in line
        if not has_marker and not has_fake:
            new_lines.append(line)
            continue
        # If the line is *only* the sentinel/fake (multiline block form),
        # drop it entirely.
        stripped = line.strip()
        if (
            stripped.startswith('"' + spec)
            or stripped.startswith(f"# {MARKER_PREFIX}:BEGIN:{marker}")
        ):
            continue
        # Inline-array form: strip the entry + marker comment from the line.
        cleaned = line
        # Drop the marker comment first (it's at EOL).
        cleaned = marker_comment_re.sub("", cleaned)
        # Now drop the dependency entry. We try a few common surroundings
        # so we don't leave dangling commas.
        for pattern in (
            f', "{spec}"',
            f',"{spec}"',
            f'"{spec}", ',
            f'"{spec}",',
            f'"{spec}"',
        ):
            if pattern in cleaned:
                cleaned = cleaned.replace(pattern, "", 1)
                break
        new_lines.append(cleaned)
    new_content = "".join(new_lines)
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Canary class
# ---------------------------------------------------------------------------


_PLANTERS = {
    "requirements.txt": _plant_requirements,
    "pyproject.toml": _plant_pyproject,
}

_UPROOTERS = {
    "requirements.txt": _uproot_requirements,
    "pyproject.toml": _uproot_pyproject,
}


class ManifestCanary:
    """Plant typosquat-trap deps in supported manifest files."""

    type_name = "manifest"

    def plant(
        self, root: Path, plant_filter: PlantFilter | None = None
    ) -> list[PlantedCanary]:
        planted: list[PlantedCanary] = []
        candidates: list[Path] = []
        for name in _MANIFEST_NAMES:
            for p in sorted(root.glob(f"**/{name}")):
                if p.is_file() and _is_tracked(p, root):
                    candidates.append(p)
        if plant_filter is not None:
            candidates = plant_filter.select(root, candidates)
        for manifest in candidates:
            planter = _PLANTERS.get(manifest.name)
            if planter is None:
                continue
            marker = _new_marker()
            if not planter(manifest, marker):
                continue
            planted.append(
                PlantedCanary(
                    id=f"manifest-{marker}",
                    type="manifest",
                    path=str(manifest.relative_to(root)),
                    marker=marker,
                )
            )
        return planted

    def uproot(self, root: Path, planted: PlantedCanary) -> None:
        target = root / planted.path
        uprooter = _UPROOTERS.get(target.name)
        if uprooter is None:
            return
        uprooter(target, planted.marker)
