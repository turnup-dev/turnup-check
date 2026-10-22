# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""LLDP via NVIDIA NVUE REST (public API), per CONTRACT.md section 3.2.

Shape modeled on the NVUE documentation (``GET /nvue_v1/interface?rev=applied``); verify on a
real switch before v0.1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from turnup_check.models import LldpNeighbor, normalize_mac

NVUE_INTERFACE_PATH = "/nvue_v1/interface"
NVUE_INTERFACE_PARAMS = {"rev": "applied"}
_SPEED = re.compile(r"(\d+)\s*([GgMm])?")
_SWITCH_PORT = re.compile(r"^(swp|Ethernet|eth\d+/|port)", re.IGNORECASE)


def parse_speed(value: Any) -> int | None:
    """``"400G"`` -> 400, ``"25G"`` -> 25, ``"100M"`` -> 0, ``400000`` (Mbps) -> 400, ``"auto"`` -> None."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value) // 1000 if value >= 1000 else int(value)
    m = _SPEED.search(str(value))
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "G").upper()
    return n if unit == "G" else n // 1000


def _is_switch_port(name: str, data: Mapping[str, Any]) -> bool:
    if data.get("type") == "swp":
        return True
    if data.get("type") in ("eth", "loopback", "bond", "svi", "vlan", "sub", "peerlink", "bridge"):
        return False
    return bool(_SWITCH_PORT.match(name))


def parse_nvue(doc: Mapping[str, Any], switch: str) -> list[LldpNeighbor]:
    """NVUE ``interface`` document -> normalized rows for every switch port."""
    rows: list[LldpNeighbor] = []
    for ifname in sorted(doc, key=_ifsort):
        data = doc.get(ifname) or {}
        if not isinstance(data, Mapping) or not _is_switch_port(ifname, data):
            continue
        link = data.get("link") or {}
        state = link.get("state")
        if isinstance(state, Mapping):
            link_up = "up" in state and "down" not in state
        else:
            link_up = str(state or "").lower() == "up"
        # ``link.speed`` on a down port is the configured speed, not a negotiated one; report speed
        # only for links that are up (eAPI likewise reports bandwidth 0 when ``notconnect``).
        speed = parse_speed(link.get("speed")) if link_up else None
        neighbors = (data.get("lldp") or {}).get("neighbor") or {}
        if not neighbors:
            rows.append(LldpNeighbor(switch=switch, local_port=ifname, link_up=link_up, speed_gbps=speed))
            continue
        for key, nb in neighbors.items():
            nb = nb or {}
            chassis = nb.get("chassis") or {}
            port = nb.get("port") or {}
            id_type = str(port.get("id-type", "")).lower()
            port_id = port.get("id")
            is_mac = id_type == "mac" or (
                not id_type and port_id is not None and normalize_mac(str(port_id)) is not None
            )
            remote_port = (
                normalize_mac(str(port_id)) if is_mac and port_id else (str(port_id) if port_id else None)
            )
            if remote_port is None and port.get("description"):
                remote_port = str(port["description"])
            system = chassis.get("system-name") or (
                key if isinstance(key, str) and not key.isdigit() else None
            )
            cid = chassis.get("chassis-id")
            rows.append(
                LldpNeighbor(
                    switch=switch,
                    local_port=ifname,
                    link_up=link_up,
                    speed_gbps=speed,
                    remote_system=system or None,
                    remote_port=remote_port,
                    remote_port_is_mac=bool(is_mac),
                    remote_chassis=normalize_mac(str(cid)) or (str(cid) if cid else None),
                )
            )
    return rows


def _ifsort(name: str) -> tuple[str, list[int]]:
    nums = [int(x) for x in re.findall(r"\d+", name)]
    return (re.sub(r"\d+", "", name), nums)


__all__ = ["NVUE_INTERFACE_PARAMS", "NVUE_INTERFACE_PATH", "parse_nvue", "parse_speed"]
