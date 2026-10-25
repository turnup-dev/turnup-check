# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Opt-in telemetry (CONTRACT.md section 5): one ``turnup.ping/v1`` after the report is written.

Sent only with ``--telemetry``. The payload carries counts and an install id, never site names,
device names, IPs or findings. Sending never raises and never changes the exit code.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from turnup_check import PING_SCHEMA, __version__
from turnup_check.models import Intent, Report, iso_z, utcnow

# CONTRACT section 5: the counter lives at counter.legent.xyz (TURNUP_COUNTER_URL overrides it).
DEFAULT_COUNTER_URL = "https://counter.legent.xyz/v1/ping"
SEND_TIMEOUT = 2.0


def config_dir() -> Path:
    """``$TURNUP_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/turnup``, else ``~/.config/turnup``."""
    override = os.environ.get("TURNUP_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "turnup"


def install_id(directory: Path | None = None) -> str:
    """Stable uuid4 per install, created on first use at ``<config_dir>/install_id``."""
    directory = directory or config_dir()
    path = directory / "install_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            uuid.UUID(existing)
            return existing
    except (OSError, ValueError):
        pass
    new = str(uuid.uuid4())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(new + "\n", encoding="utf-8")
    except OSError:
        pass  # a read-only home still gets a (non-persistent) id
    return new


def build_ping(report: Report, intent: Intent | None = None, *, install: str | None = None) -> dict[str, Any]:
    """The exact payload of CONTRACT section 5."""
    racks = report.intent.get("racks", 0) if isinstance(report.intent, dict) else 0
    if intent is not None:
        racks = len(intent.rack_names)
    return {
        "schema": PING_SCHEMA,
        "install_id": install or install_id(),
        "tool_version": __version__,
        "simulated": bool(report.simulated),
        "racks_checked": int(racks),
        "links_verified": int(report.summary.links_ok),
        "nodes_checked": int(report.summary.nodes_total),
        "ts": iso_z(utcnow()),
    }


def counter_url() -> str:
    return os.environ.get("TURNUP_COUNTER_URL") or DEFAULT_COUNTER_URL


def send_ping(
    payload: dict[str, Any],
    *,
    url: str | None = None,
    timeout: float = SEND_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str]:
    """POST the ping. Returns ``(ok, message)``; never raises."""
    target = url or counter_url()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(target, json=payload)
        if 200 <= resp.status_code < 300:
            racks, links = int(payload.get("racks_checked", 0)), int(payload.get("links_verified", 0))
            return (
                True,
                f"sent ({racks} rack{'s' if racks != 1 else ''}, {links} link{'s' if links != 1 else ''})",
            )
        return False, f"counter answered HTTP {resp.status_code} (ignored)"
    except Exception as exc:
        return False, f"could not reach counter ({exc.__class__.__name__}; ignored)"


__all__ = [
    "DEFAULT_COUNTER_URL",
    "SEND_TIMEOUT",
    "build_ping",
    "config_dir",
    "counter_url",
    "install_id",
    "send_ping",
]
