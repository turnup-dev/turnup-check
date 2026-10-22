# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Nautobot intent adapter: the NetBox mapping with ``location=`` and ``role.name`` (CONTRACT 2.1).

Nautobot (2.x and 3.x) returns nested objects as bare ``{id, object_type, url}`` references unless
``?depth=`` is given. Verified against demo.nautobot.com 3.2.7 on 2026-10-16:

- devices: ``depth=2`` so ``device_type.manufacturer.name`` (the LLDP protocol choice) and
  ``primary_ip4.address`` (the mgmt URL fallback) are filled in; at ``depth=1`` both are bare
  references. ``include=config_context`` because list views omit it (the firmware baseline).
- interfaces: ``depth=1`` (``device.name`` is present).
- cables: ``depth=1``. ``depth=2`` costs ~0.8 s per cable server-side on the demo and timed out the
  whole intent load; at ``depth=1`` each termination carries its interface name and a device
  reference, which is resolved against the device list already read.
"""

from __future__ import annotations

from typing import Any

from turnup_check.intent.netbox import NetBoxIntentSource, _get

DEPTH: dict[str, int] = {"devices": 2, "interfaces": 1, "cables": 1, "racks": 1, "locations": 1}


class NautobotIntentSource(NetBoxIntentSource):
    flavor = "nautobot"
    site_filter = "location"
    site_endpoint = "locations"

    def _extra_params(self, endpoint: str) -> dict[str, Any]:
        params: dict[str, Any] = {"depth": DEPTH.get(endpoint, 1)}
        if endpoint == "devices":
            params["include"] = "config_context"
        return params

    async def _paged(self, client: Any, endpoint: str, params: dict[str, Any]) -> list[dict]:
        if endpoint == "locations" and "slug" in params:  # Nautobot 2+ locations have names, not slugs
            params = {"name": params["slug"]}
        return await super()._paged(client, endpoint, params)

    def raw_role(self, dev: dict) -> str | None:
        return _get(dev, "role", "name") or _get(dev, "role", "slug") or _get(dev, "device_role", "slug")
