"""Pure validation and lookup helpers for the named Claude-Hub catalog."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath


CATALOG_VERSION = 1
LEGACY_HUB_ID = "claude-hub"
MAX_DISPLAY_NAME_WIDTH = 48

_HUB_ID_RE = re.compile(r"[a-z][a-z0-9-]*\Z")
_ENTRY_PATH_FIELDS = ("config", "log", "usage")
_ENTRY_STATES = {"ready", "setup"}


def legacy_hub_entry(
    *,
    name: str = "Claude-Hub",
    config: str = "claude-hub.json",
    log: str = "logs/claude-hub.log",
    usage: str = "logs/claude-hub-usage.jsonl",
) -> dict[str, str]:
    """Build the catalog entry for the existing single-Hub installation."""
    entry = {"name": name, "config": config, "log": log, "usage": usage}
    probe = {
        "version": CATALOG_VERSION,
        "default_hub": LEGACY_HUB_ID,
        "order": [LEGACY_HUB_ID],
        "hubs": {LEGACY_HUB_ID: entry},
    }
    return normalize_hub_catalog(probe)["hubs"][LEGACY_HUB_ID]


def normalize_hub_catalog(raw: object) -> dict:
    """Validate and return a detached catalog while preserving mapping order."""
    if not isinstance(raw, dict):
        raise ValueError("hub catalog root must be an object")
    catalog = copy.deepcopy(raw)
    if type(catalog.get("version")) is not int or catalog["version"] != CATALOG_VERSION:
        raise ValueError("hub catalog version must be 1")
    hubs = catalog.get("hubs")
    if not isinstance(hubs, dict) or not hubs:
        raise ValueError("hub catalog hubs must be a non-empty object")
    default_hub = catalog.get("default_hub")
    if not isinstance(default_hub, str) or default_hub not in hubs:
        raise ValueError("hub catalog default_hub must reference hubs")
    order = catalog.get("order")
    if (
        not isinstance(order, list)
        or any(not isinstance(hub_id, str) for hub_id in order)
        or len(order) != len(hubs)
        or len(set(order)) != len(order)
        or set(order) != set(hubs)
    ):
        raise ValueError("hub catalog order must list every hub id exactly once")
    display_name_owners: dict[str, str] = {}
    path_owners: dict[str, str] = {}
    for hub_id, entry in hubs.items():
        if not isinstance(hub_id, str) or _HUB_ID_RE.fullmatch(hub_id) is None:
            raise ValueError(f"invalid hub id: {hub_id!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"hubs.{hub_id} must be an object")
        entry["name"] = validate_display_name(entry.get("name"))
        folded_name = entry["name"].casefold()
        previous_owner = display_name_owners.get(folded_name)
        if previous_owner is not None:
            raise ValueError(
                "hub display names must be unique (case-insensitive): "
                f"{previous_owner} and {hub_id}"
            )
        display_name_owners[folded_name] = hub_id
        state = entry.get("state", "ready")
        if not isinstance(state, str) or state not in _ENTRY_STATES:
            raise ValueError(f"hubs.{hub_id}.state must be ready or setup")
        entry["state"] = state
        for field in _ENTRY_PATH_FIELDS:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"hubs.{hub_id}.{field} must be a relative path")
            value = value.strip()
            _validate_relative_path(value)
            canonical = PurePosixPath(value.replace("\\", "/")).as_posix().casefold()
            owner = path_owners.get(canonical)
            if owner is not None:
                raise ValueError(
                    "hub catalog path is shared; paths must be unique: "
                    f"{owner} and {hub_id}.{field} share {value}"
                )
            path_owners[canonical] = f"{hub_id}.{field}"
            entry[field] = value
        draft = entry.get("draft")
        if state == "setup" and draft is None:
            raise ValueError(f"hubs.{hub_id}.draft is required while setup")
        if draft is not None:
            if not isinstance(draft, str) or not draft.strip():
                raise ValueError(f"hubs.{hub_id}.draft must be a relative path")
            draft = draft.strip()
            _validate_relative_path(draft)
            canonical = PurePosixPath(draft.replace("\\", "/")).as_posix().casefold()
            owner = path_owners.get(canonical)
            if owner is not None:
                raise ValueError(
                    "hub catalog path is shared; paths must be unique: "
                    f"{owner} and {hub_id}.draft share {draft}"
                )
            path_owners[canonical] = f"{hub_id}.draft"
            entry["draft"] = draft
    return catalog


def load_hub_catalog(raw: object) -> dict:
    """Load an already parsed JSON value through catalog normalization."""
    return normalize_hub_catalog(raw)


def validate_display_name(value: object) -> str:
    """Return a trimmed, terminal-safe Hub display name."""
    if not isinstance(value, str):
        raise ValueError("hub display name must be a string")
    name = value.strip()
    if not name:
        raise ValueError("hub display name must not be empty")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("hub display name must not contain control characters")
    width = _display_width(name)
    if width < 1:
        raise ValueError("hub display name must occupy at least one column")
    if width > MAX_DISPLAY_NAME_WIDTH:
        raise ValueError(
            f"hub display name must not exceed {MAX_DISPLAY_NAME_WIDTH} columns"
        )
    return name


def resolve_hub_id(catalog: Mapping[str, object], value: str | None = None) -> str:
    """Resolve the default Hub, a stable id, or one unique display name."""
    if value is None:
        default_hub = catalog.get("default_hub")
        if isinstance(default_hub, str):
            return default_hub
        raise ValueError("hub catalog has no valid default_hub")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hub id or display name must not be empty")
    hubs = catalog.get("hubs")
    if not isinstance(hubs, Mapping):
        raise ValueError("hub catalog has no valid hubs")
    query = value.strip()
    folded_id = query.casefold()
    if folded_id in hubs:
        return folded_id
    matches = [
        str(hub_id)
        for hub_id, entry in hubs.items()
        if isinstance(entry, Mapping)
        and isinstance(entry.get("name"), str)
        and entry["name"].casefold() == query.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"hub display name is ambiguous: {query}")
    raise ValueError(f"hub not found: {query}")


def unique_hub_id(name: str, existing: Iterable[str]) -> str:
    """Create a deterministic ASCII Hub id not present in ``existing``."""
    display_name = validate_display_name(name)
    ascii_name = (
        unicodedata.normalize("NFKD", display_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not slug:
        slug = "hub"
    elif not slug[0].isalpha():
        slug = f"hub-{slug}"
    occupied = {str(hub_id).casefold() for hub_id in existing}
    if slug not in occupied:
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in occupied:
        suffix += 1
    return f"{slug}-{suffix}"


def resolve_catalog_path(catalog_path: str | Path, relative_path: str) -> Path:
    """Resolve one catalog entry path lexically beneath the catalog directory."""
    if not isinstance(relative_path, str):
        raise ValueError("hub catalog path must be a string")
    value = relative_path.strip()
    _validate_relative_path(value)
    return Path(catalog_path).parent / Path(value)


def validate_unique_hub_ports(
    configs: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    """Validate loaded Hub ports and return their normalized integer values."""
    ports: dict[str, int] = {}
    owners: dict[int, str] = {}
    for hub_id, config in configs.items():
        if not isinstance(config, Mapping):
            raise ValueError(f"hub config must be an object: {hub_id}")
        raw = config.get("port")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"hub port is invalid: {hub_id}")
        if isinstance(raw, str) and not raw.strip().isdigit():
            raise ValueError(f"hub port is invalid: {hub_id}")
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError(f"hub port is invalid: {hub_id}")
        previous = owners.get(port)
        if previous is not None:
            raise ValueError(
                f"hub port conflict: {previous} and {hub_id} both use {port}"
            )
        owners[port] = str(hub_id)
        ports[str(hub_id)] = port
    return ports


def _display_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def _validate_relative_path(value: str) -> None:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError("hub catalog paths must be safe relative paths")
    if value in {"", "."} or posix.name in {"", ".", ".."}:
        raise ValueError("hub catalog paths must name a file")
