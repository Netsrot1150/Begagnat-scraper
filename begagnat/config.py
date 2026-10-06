"""YAML config: watch profiles, notification targets, and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .query import Criteria

DEFAULT_CONFIG_PATHS = [
    Path("begagnat.yaml"),
    Path.home() / ".config" / "begagnat" / "config.yaml",
]


@dataclass
class Profile:
    """One named search that ``begagnat watch`` runs on every pass."""

    name: str
    criteria: Criteria
    source_options: dict = field(default_factory=dict)


@dataclass
class Config:
    profiles: list[Profile] = field(default_factory=list)
    notify: dict = field(default_factory=lambda: {"console": True})
    fx: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    source_options: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
        for candidate in candidates:
            if candidate.exists():
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                return cls.from_dict(data)
        if path:
            raise FileNotFoundError(f"config file not found: {path}")
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        defaults = data.get("defaults", {}) or {}
        source_options = data.get("source_options", {}) or {}
        profiles = []
        for entry in data.get("profiles", []) or []:
            merged = {**defaults, **entry}
            name = merged.pop("name", merged.get("query", "profile"))
            merged.pop("source_options", None)
            profiles.append(
                Profile(
                    name=name,
                    criteria=criteria_from_dict(merged),
                    source_options={**source_options, **(entry.get("source_options") or {})},
                )
            )
        return cls(
            profiles=profiles,
            notify=data.get("notify", {"console": True}) or {},
            fx=data.get("fx", {}) or {},
            ai=data.get("ai", {}) or {},
            defaults=defaults,
            source_options=source_options,
        )


def criteria_from_dict(data: dict) -> Criteria:
    """Build a Criteria from config keys, ignoring anything we do not know."""
    fields = {f for f in Criteria.__dataclass_fields__}
    known = {k: v for k, v in data.items() if k in fields}
    for list_field in ("keywords", "sources", "models", "exclude"):
        value = known.get(list_field)
        if isinstance(value, str):
            known[list_field] = [value]
    return Criteria(**known)
