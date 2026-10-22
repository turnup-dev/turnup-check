# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""LLDP via Arista eAPI (public JSON-RPC), per CONTRACT.md section 3.2."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from turnup_check.models import LldpNeighbor, normalize_mac

EAPI_COMMANDS = ["show lldp neighbors detail", "show interfaces status"]


def eapi_request_body() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "runCmds",
        "params": {"version": 1, "cmds": list(EAPI_COMMANDS), "format": "json"},
        "id": "turnup",
    }


def parse_eapi(result: list[Any] | Mapping[str, Any], switch: str) -> list[LldpNeighbor]:
    """eAPI ``result`` (``[lldp-detail, interfaces-status]``) -> normalized rows.

    Also accepts the whole JSON-RPC response object, or one object carrying both ``lldpNeighbors`` and
    ``interfaceStatuses``. Every interface in either command gets a row; interfaces with several
    neighbors get one row per neighbor. ``bandwidth`` is bits per second. Raises ``ValueError`` when
    neighbors come without any interface status (the link state of every port would be a guess).
    """
    if isinstance(result, Mapping):
        if "lldpNeighbors" in result or "interfaceStatuses" in result:  # both command bodies in one object
            result = [result, result]
        else:  # a whole JSON-RPC response object
            result = result.get("result") or []
    lldp_doc: Mapping[str, Any] = result[0] if len(result) > 0 and isinstance(result[0], Mapping) else {}
    status_doc: Mapping[str, Any] = result[1] if len(result) > 1 and isinstance(result[1], Mapping) else {}
    neighbors: Mapping[str, Any] = lldp_doc.get("lldpNeighbors") or {}
    statuses: Mapping[str, Any] = status_doc.get("interfaceStatuses") or {}

    if not statuses and any((v or {}).get("lldpNeighborInfo") for v in neighbors.values()):
        # Neighbors without any interface status: link state is unknown, not "every port down".
        raise ValueError("eapi: lldp neighbors but no interface status (show interfaces status empty)")
    rows: list[LldpNeighbor] = []
    for ifname in sorted(set(neighbors) | set(statuses), key=_ifsort):
        st = statuses.get(ifname) or {}
        link_up = str(st.get("linkStatus", "")).lower() == "connected"
        bw = st.get("bandwidth")
        speed = round(bw / 1e9) if isinstance(bw, int | float) and bw > 0 else None
        infos = (neighbors.get(ifname) or {}).get("lldpNeighborInfo") or []
        if not infos:
            rows.append(LldpNeighbor(switch=switch, local_port=ifname, link_up=link_up, speed_gbps=speed))
            continue
        for info in infos:
            nii = info.get("neighborInterfaceInfo") or {}
            port_id = nii.get("interfaceId")
            id_type = str(nii.get("interfaceIdType", "")).lower()
            is_mac = id_type == "macaddress" or (
                port_id is not None and normalize_mac(str(port_id)) is not None and id_type != "interfacename"
            )
            remote_port = (
                normalize_mac(str(port_id)) if is_mac and port_id else (str(port_id) if port_id else None)
            )
            if remote_port is None and nii.get("interfaceDescription"):
                remote_port = str(nii["interfaceDescription"])
            chassis = info.get("chassisId")
            rows.append(
                LldpNeighbor(
                    switch=switch,
                    local_port=ifname,
                    link_up=link_up,
                    speed_gbps=speed,
                    remote_system=info.get("systemName") or None,
                    remote_port=remote_port,
                    remote_port_is_mac=bool(is_mac),
                    remote_chassis=normalize_mac(str(chassis)) or (str(chassis) if chassis else None),
                )
            )
    return rows


def _ifsort(name: str) -> tuple[str, list[int]]:
    import re

    nums = [int(x) for x in re.findall(r"\d+", name)]
    return (re.sub(r"\d+", "", name), nums)


__all__ = ["EAPI_COMMANDS", "eapi_request_body", "parse_eapi"]
