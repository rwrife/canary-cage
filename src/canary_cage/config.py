"""Canary-cage configuration: ``canary.toml`` schema + presets.

The config lives at the repo root as ``canary.toml`` and is intentionally
small: pick which canary *types* to plant, which paths to *ignore*, and
how *densely* to plant within the eligible set. Three named presets keep
the common cases one keystroke away.

``canary init`` writes a commented default config so users can discover
the knobs without spelunking the docs.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CONFIG_FILE_NAME = "canary.toml"

CanaryType = Literal["markdown", "docstring", "todo"]
ALL_TYPES: tuple[CanaryType, ...] = ("markdown", "docstring", "todo")

PresetName = Literal["minimal", "paranoid", "chaotic-good"]

# Always ignore the cage's own state directory + anything in dot-dirs.
# These are layered on top of user-supplied ignores.
DEFAULT_IGNORE: tuple[str, ...] = (
    ".canary-cage/**",
    ".git/**",
)


class WebhookConfig(BaseModel):
    """Optional webhook beacon configuration.

    When ``url`` is set, ``canary check`` will POST every fire to that
    URL in addition to the always-on file/log beacons. Missing / empty
    config keeps the beacon disabled (the default for v0.1).
    """

    url: str | None = None
    timeout: float = 5.0
    max_attempts: int = 3
    backoff: float = 0.5
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("timeout")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout must be > 0")
        return v

    @field_validator("max_attempts")
    @classmethod
    def _attempts_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_attempts must be >= 1")
        return v


class ChatBeaconConfig(BaseModel):
    """Slack or Discord incoming-webhook beacon configuration.

    When ``url`` is set, ``canary check`` will POST a short, rendered
    message per fire to the configured chat webhook in addition to the
    always-on file/log beacons.
    """

    url: str | None = None
    timeout: float = 5.0
    max_attempts: int = 3
    backoff: float = 0.5
    headers: dict[str, str] = Field(default_factory=dict)
    snippet_chars: int = 240

    @field_validator("timeout")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout must be > 0")
        return v

    @field_validator("max_attempts")
    @classmethod
    def _attempts_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_attempts must be >= 1")
        return v

    @field_validator("snippet_chars")
    @classmethod
    def _snippet_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError("snippet_chars must be >= 0")
        return v


class CageConfig(BaseModel):
    """Top-level config document loaded from ``canary.toml``.

    All fields have sensible defaults so an empty/missing config behaves
    exactly like the historical "plant everything everywhere" mode.
    """

    preset: PresetName | None = None
    types: list[CanaryType] = Field(default_factory=lambda: list(ALL_TYPES))
    ignore: list[str] = Field(default_factory=list)
    density: float = 1.0
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    slack: ChatBeaconConfig = Field(default_factory=ChatBeaconConfig)
    discord: ChatBeaconConfig = Field(default_factory=ChatBeaconConfig)

    @field_validator("density")
    @classmethod
    def _density_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("density must be between 0.0 and 1.0")
        return v

    @field_validator("types")
    @classmethod
    def _types_nonempty(cls, v: list[CanaryType]) -> list[CanaryType]:
        if not v:
            raise ValueError("types must contain at least one canary type")
        return v


# Preset definitions. ``preset`` in the config wins over the per-field
# defaults but loses to explicit per-field overrides in the same file.
PRESETS: dict[PresetName, dict[str, object]] = {
    "minimal": {
        "types": ["markdown"],
        "density": 0.25,
    },
    "paranoid": {
        "types": list(ALL_TYPES),
        "density": 1.0,
    },
    "chaotic-good": {
        "types": list(ALL_TYPES),
        "density": 0.5,
    },
}


def config_path(root: Path) -> Path:
    return root / CONFIG_FILE_NAME


def _apply_preset(
    raw: dict[str, object], preset: PresetName
) -> dict[str, object]:
    """Layer preset defaults under any explicit ``raw`` keys."""
    merged = dict(PRESETS[preset])
    merged.update(raw)
    merged["preset"] = preset
    return merged


def load_config(root: Path) -> CageConfig:
    """Load ``canary.toml`` from ``root`` (or return defaults)."""
    path = config_path(root)
    if not path.exists():
        return CageConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    section = data.get("canary", data)
    if not isinstance(section, dict):
        raise ValueError(f"{CONFIG_FILE_NAME}: [canary] section must be a table")
    # Surface a top-level [beacons.webhook] table as canary.webhook so
    # users can keep the beacon config visually separate from canary
    # planting knobs in their TOML.
    beacons = data.get("beacons")
    if isinstance(beacons, dict):
        for key in ("webhook", "slack", "discord"):
            sub = beacons.get(key)
            if isinstance(sub, dict) and key not in section:
                section = {**section, key: sub}
    preset = section.get("preset")
    if preset is not None:
        if preset not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ValueError(
                f"{CONFIG_FILE_NAME}: unknown preset {preset!r} (known: {known})"
            )
        section = _apply_preset(
            {k: v for k, v in section.items() if k != "preset"}, preset  # type: ignore[arg-type]
        )
    return CageConfig.model_validate(section)


DEFAULT_CONFIG_TEMPLATE = """\
# canary-cage config — see https://github.com/rwrife/canary-cage
#
# Pick a preset, or hand-tune the fields below. Explicit fields always
# win over preset defaults.

[canary]
# preset = "minimal"       # markdown-only, low density
# preset = "chaotic-good"  # all types, ~half of eligible files
# preset = "paranoid"      # all types, everywhere

# Canary types to plant. Subset of: "markdown", "docstring", "todo".
types = ["markdown", "docstring", "todo"]

# Glob patterns (relative to repo root) to skip when planting.
# .canary-cage/** and .git/** are always ignored.
ignore = [
    # "docs/**",
    # "vendor/**",
]

# Fraction of eligible files (per type) to actually plant in: 0.0–1.0.
density = 1.0

# Optional webhook beacon. When `url` is set, `canary check` POSTs every
# detected fire as JSON to the URL (file + log beacons still run too).
# [beacons.webhook]
# url = "https://example.com/canary-fires"
# timeout = 5.0
# max_attempts = 3
# backoff = 0.5
# headers = { Authorization = "Bearer ${CANARY_WEBHOOK_TOKEN}" }

# Optional Slack incoming-webhook beacon. When `url` is set, every fire
# is rendered as a short message and POSTed to the channel.
# [beacons.slack]
# url = "https://hooks.slack.com/services/T000/B000/XXX"
# snippet_chars = 240

# Optional Discord webhook beacon. Same shape as Slack — set `url` and
# every fire pings the channel.
# [beacons.discord]
# url = "https://discord.com/api/webhooks/000/XXX"
# snippet_chars = 240
"""


def write_default_config(root: Path, *, overwrite: bool = False) -> Path:
    """Write a commented default ``canary.toml`` and return its path."""
    path = config_path(root)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Plant-time helpers used by canary modules.
# ---------------------------------------------------------------------------


class PlantFilter:
    """File-selection policy derived from a :class:`CageConfig`.

    Canary modules build a sorted candidate list of files and pass it
    through :meth:`select` to honour the user's ignore globs + density.
    """

    def __init__(self, config: CageConfig) -> None:
        self.config = config
        self._ignore_globs: tuple[str, ...] = tuple(
            list(DEFAULT_IGNORE) + list(config.ignore)
        )

    def is_ignored(self, rel: str) -> bool:
        rel = rel.replace("\\", "/")
        for pat in self._ignore_globs:
            if fnmatch.fnmatch(rel, pat):
                return True
            # Allow matching directory prefixes like "docs/" via "docs/**".
            if pat.endswith("/**") and (
                rel == pat[:-3] or rel.startswith(pat[:-2])
            ):
                return True
        return False

    def select(self, root: Path, candidates: Iterable[Path]) -> list[Path]:
        """Filter ``candidates`` by ignore globs + density (deterministic)."""
        kept: list[Path] = []
        for p in candidates:
            try:
                rel = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if self.is_ignored(rel):
                continue
            kept.append(p)
        return _apply_density(kept, self.config.density)


def _apply_density(files: Sequence[Path], density: float) -> list[Path]:
    """Deterministically keep ``ceil(len(files) * density)`` entries."""
    if density >= 1.0 or not files:
        return list(files)
    if density <= 0.0:
        return []
    import math

    n = max(1, math.ceil(len(files) * density))
    # Sort for determinism; pick a stride so the kept set is spread out.
    ordered = sorted(files)
    if n >= len(ordered):
        return ordered
    stride = len(ordered) / n
    picked = [ordered[int(i * stride)] for i in range(n)]
    # De-dup while preserving order (stride math is safe but defensive).
    seen: set[Path] = set()
    out: list[Path] = []
    for p in picked:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
