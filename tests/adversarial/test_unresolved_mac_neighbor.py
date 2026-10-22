# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A neighbor that cannot be resolved to a port must not become "seat cable" on a port that shows it.

MAC-advertising NICs, no MACs in intent (NetBox often has none) and the node's BMC unreachable: the
leaf shows node2 (system name and chassis id resolve to node2) on every intended port, but the port id
is a MAC nobody can map. The diff reports, for each of node2's four cables, both `missing_link` with
fix "seat cable ..." (CONTRACT 4 defines missing_link as "intended port shows no neighbor") and an
`unexpected_link` on the same intended port. The cables are seated correctly; nothing should tell a
technician to re-seat them.
"""

from __future__ import annotations

import pytest
from adv_helpers import Hall, by_class, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def test_unresolved_mac_from_intended_node_is_not_missing_link():
    hall = Hall.generate(small_spec(lldp_port_id_mode="mac"))
    intent = hall.intent()
    for dev in intent["devices"]:
        for itf in dev["interfaces"]:
            itf.pop("mac", None)
    inject(hall, "unreachable_node", node="node2", mode="timeout")
    res = run_check(hall, intent=intent)
    assert [f.device for f in by_class(res, "unreachable_node")] == ["node2"]
    assert by_class(res, "missing_link") == [], describe(res)
    assert any("node2" in n for n in res.notes), res.notes
