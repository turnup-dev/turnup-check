# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""SNMP LLDP-MIB parser over canned OID tables (no SNMP traffic in tests)."""

from __future__ import annotations

import yaml
from helpers import FIXTURES

from turnup_check.collectors.lldp_snmp import (
    IF_HIGH_SPEED,
    IF_NAME,
    IF_OPER_STATUS,
    LLDP_LOC_PORT_TABLE,
    LLDP_REM_TABLE,
    WALK_ROOTS,
    collect_lldp_snmp,
    host_from_url,
    parse_lldp_snmp,
)


def canned_rows() -> list[tuple[str, object]]:
    doc = yaml.safe_load((FIXTURES / "snmp" / "leaf5_lldp.yaml").read_text(encoding="utf-8"))
    return [(oid, value) for oid, value in doc["rows"]]


def test_parse_canned_table():
    rows = {r.local_port: r for r in parse_lldp_snmp(canned_rows(), "leaf5")}
    assert sorted(rows) == ["Ethernet21", "Ethernet22", "Ethernet23", "Ethernet24"]
    e21 = rows["Ethernet21"]
    assert e21.switch == "leaf5" and e21.link_up is True and e21.speed_gbps == 400
    assert (e21.remote_system, e21.remote_port, e21.remote_port_is_mac) == ("node7", "mlx5_3", False)
    assert e21.remote_chassis == "b8:3f:d2:00:07:00"
    e22 = rows["Ethernet22"]
    assert e22.link_up is False and e22.has_neighbor is False and e22.speed_gbps is None
    e23 = rows["Ethernet23"]
    assert e23.remote_port_is_mac is True and e23.remote_port == "b8:3f:d2:00:08:01" and e23.speed_gbps == 200
    assert rows["Ethernet24"].link_up is True and rows["Ethernet24"].has_neighbor is False


def test_parse_raw_bytes_values_as_pysnmp_yields_them():
    rows = [
        (f"{LLDP_LOC_PORT_TABLE}.2.7", 5),
        (f"{LLDP_LOC_PORT_TABLE}.3.7", b"swp7"),
        (f"{LLDP_REM_TABLE}.4.0.7.1", 4),
        (f"{LLDP_REM_TABLE}.5.0.7.1", bytes.fromhex("b83fd2000700")),
        (f"{LLDP_REM_TABLE}.6.0.7.1", 3),
        (f"{LLDP_REM_TABLE}.7.0.7.1", bytes.fromhex("b83fd2000703")),
        (f"{LLDP_REM_TABLE}.9.0.7.1", b"node7.hall.example"),
        (f"{IF_NAME}.7", b"swp7"),
        (f"{IF_OPER_STATUS}.7", 1),
        (f"{IF_HIGH_SPEED}.7", 400000),
    ]
    (row,) = parse_lldp_snmp(rows, "leaf6")
    assert row.local_port == "swp7" and row.link_up is True and row.speed_gbps == 400
    assert row.remote_port == "b8:3f:d2:00:07:03" and row.remote_port_is_mac is True
    assert row.remote_chassis == "b8:3f:d2:00:07:00"
    assert row.remote_system == "node7.hall.example"


def test_port_name_falls_back_to_ifname_when_loc_port_id_is_a_mac():
    rows = [
        (f"{LLDP_LOC_PORT_TABLE}.2.3", 3),
        (f"{LLDP_LOC_PORT_TABLE}.3.3", bytes.fromhex("001122334455")),
        (f"{IF_NAME}.3", "Ethernet3"),
        (f"{IF_OPER_STATUS}.3", 2),
    ]
    (row,) = parse_lldp_snmp(rows, "s")
    assert row.local_port == "Ethernet3" and row.link_up is False


def test_link_state_defaults_to_neighbor_presence_without_if_mib():
    rows = [
        (f"{LLDP_LOC_PORT_TABLE}.3.1", "swp1"),
        (f"{LLDP_LOC_PORT_TABLE}.3.2", "swp2"),
        (f"{LLDP_REM_TABLE}.6.0.1.1", 5),
        (f"{LLDP_REM_TABLE}.7.0.1.1", "mlx5_0"),
        (f"{LLDP_REM_TABLE}.9.0.1.1", "node1"),
    ]
    by = {r.local_port: r for r in parse_lldp_snmp(rows, "s")}
    assert by["swp1"].link_up is True and by["swp2"].link_up is False


async def test_collect_with_injected_walker_walks_every_root():
    walked: list[str] = []
    table = canned_rows()

    async def walker(root: str):
        walked.append(root)
        return [(oid, v) for oid, v in table if oid.startswith(root)]

    rows = await collect_lldp_snmp("leaf5", "10.0.0.5", "public", 1.0, walker=walker)
    assert walked == list(WALK_ROOTS)
    assert len(rows) == 4


def test_host_from_url():
    assert host_from_url("https://10.0.1.4") == "10.0.1.4"
    assert host_from_url("http://127.0.0.1:8900/dev/leaf4") == "127.0.0.1"
    assert host_from_url("leaf4.mgmt") == "leaf4.mgmt"
