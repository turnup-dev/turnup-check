# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Intent adapters: load a ``turnup.intent/v1`` document from a file, NetBox or Nautobot.

``--intent netbox`` and ``--intent nautobot`` read ``NETBOX_URL``/``NETBOX_TOKEN`` and
``NAUTOBOT_URL``/``NAUTOBOT_TOKEN``. ``--intent http://host/netbox`` treats the URL as a NetBox
base URL (token from ``NETBOX_TOKEN`` when set). Anything else is a YAML/JSON file path,
optionally prefixed with ``file:``.

NetBox/Nautobot reads use a per-request read timeout of at least ``INTENT_TIMEOUT`` seconds (a
Nautobot cable list can take a minute server-side); connecting is still capped at 10 s. Notes from
the adapter (skipped cables and why, unrecognised roles, logical interfaces left out) are put on
``Intent.notes``.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from turnup_check.intent.file import load_intent_file, merge_baseline_file
from turnup_check.intent.nautobot import NautobotIntentSource
from turnup_check.intent.netbox import NetBoxIntentSource
from turnup_check.models import Intent

INTENT_TIMEOUT = 120.0


class IntentError(Exception):
    """The intent could not be read. The CLI maps this to exit code 2."""


def describe_intent_arg(arg: str) -> str:
    """Short label for the ``source`` field of the report."""
    low = arg.strip().lower()
    if low == "netbox" or low.startswith(("http://", "https://")):
        return "netbox"
    if low == "nautobot":
        return "nautobot"
    return "file"


async def load_intent(
    arg: str,
    *,
    site: str | None,
    baseline: str | Path | None = None,
    timeout: float = INTENT_TIMEOUT,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
) -> Intent:
    """Load intent for ``--intent <arg>`` and apply ``--baseline`` when given."""
    env = dict(os.environ if env is None else env)
    low = arg.strip().lower()
    try:
        if low == "netbox":
            url = env.get("NETBOX_URL")
            if not url:
                raise IntentError("--intent netbox needs NETBOX_URL (and NETBOX_TOKEN) in the environment")
            intent = await _load_netbox(url, env.get("NETBOX_TOKEN"), site, timeout, client)
        elif low.startswith(("http://", "https://")):
            intent = await _load_netbox(arg.strip(), env.get("NETBOX_TOKEN"), site, timeout, client)
        elif low == "nautobot":
            url = env.get("NAUTOBOT_URL")
            if not url:
                raise IntentError(
                    "--intent nautobot needs NAUTOBOT_URL (and NAUTOBOT_TOKEN) in the environment"
                )
            if not site:
                raise IntentError("--site is required with --intent nautobot")
            src = NautobotIntentSource(url, env.get("NAUTOBOT_TOKEN"), site, timeout=timeout, client=client)
            intent = await src.load()
            intent.notes = list(src.notes)
        else:
            path = arg.strip()
            if path.lower().startswith("file:"):
                path = path[5:]
            intent = load_intent_file(path)
    except IntentError:
        raise
    except httpx.HTTPError as exc:  # transport, status, decoding
        raise IntentError(f"{describe_intent_arg(arg)}: {exc.__class__.__name__}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise IntentError(f"{describe_intent_arg(arg)}: {exc}") from exc

    if baseline:
        try:
            merge_baseline_file(intent, baseline)
        except (OSError, ValueError) as exc:
            raise IntentError(f"baseline: {exc}") from exc
    return intent


async def _load_netbox(
    url: str, token: str | None, site: str | None, timeout: float, client: httpx.AsyncClient | None
) -> Intent:
    if not site:
        raise IntentError("--site is required when the intent comes from NetBox")
    src = NetBoxIntentSource(url, token, site, timeout=timeout, client=client)
    intent = await src.load()
    intent.notes = list(src.notes)
    return intent


__all__ = [
    "INTENT_TIMEOUT",
    "IntentError",
    "NautobotIntentSource",
    "NetBoxIntentSource",
    "describe_intent_arg",
    "load_intent",
    "load_intent_file",
    "merge_baseline_file",
]
