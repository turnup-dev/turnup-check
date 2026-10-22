# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""MAC resolution through Redfish must map to the intent interface the port matches (Id, then Name).

CONTRACT 3.1: a NIC port's `Id` equals the intent interface name "or the emulator sets `Name` to
it; collector matches by Id, then Name, then MAC". BMCs that number their ports (`Id: "1"`,
`Name: "mlx5_0"`) are common. With MAC-advertising NICs and no MACs in intent, `_Index` maps each
Redfish MAC to `(node, port.Id)` = `(node1, "1")`, which is not an intent interface: a correctly
cabled hall reports 16 missing_link ("seat cable ...") plus 16 unexpected_link.
"""

from __future__ import annotations

import copy

import pytest
from adv_helpers import Hall, classes, describe, run_check, small_spec

pytestmark = pytest.mark.integration


def _numeric_port_ids(node: str, tree: dict) -> dict:
    tree = copy.deepcopy(tree)
    for path, res in tree.items():
        if "/Ports/" in path and isinstance(res, dict) and "Id" in res:
            res["Name"] = res["Id"]
            res["Id"] = str(int(res["Id"].split("_")[1]) + 1)
    return tree


def test_clean_mac_hall_with_numbered_redfish_ports_has_no_findings():
    hall = Hall.generate(small_spec(lldp_port_id_mode="mac"))
    intent = hall.intent()
    for dev in intent["devices"]:
        for itf in dev["interfaces"]:
            itf.pop("mac", None)
    res = run_check(hall, intent=intent, redfish_hook=_numeric_port_ids)
    assert classes(res) == [], describe(res)
    assert res.summary.links_ok == res.summary.links_total
