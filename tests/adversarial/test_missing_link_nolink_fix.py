# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""An unseated cable (node port reports NoLink) must get the "seat cable" fix, not "check NIC".

CONTRACT 4: missing_link fix is `seat cable <label> at <S>/<P>`, or `check NIC mlx5_x on <node>` only
"when the node port also reports LinkDown". Redfish LinkStatus distinguishes NoLink (nothing
connected) from LinkDown (connected, link down); simhall serves NoLink for an unseated cable. The diff
treats any non-LinkUp status as "NIC down", so every unseated cable is sent to the NIC.
"""

from __future__ import annotations

import pytest
from adv_helpers import Hall, by_class, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def test_unseated_cable_with_nolink_nic_says_seat_cable():
    hall = Hall.generate(small_spec())
    inject(hall, "missing_link", cable="CBL-12-01-00")
    res = run_check(hall)
    missing = by_class(res, "missing_link")
    assert len(missing) == 1, describe(res)
    assert missing[0].evidence["node1 mlx5_0"]["link_up"] is False  # NoLink, observed in pass two
    assert missing[0].fix == "seat cable CBL-12-01-00 at leaf3/1", describe(res)


def test_link_down_nic_still_says_check_nic():
    """Control: a real LinkDown keeps the existing (correct) fix."""
    hall = Hall.generate(small_spec())
    inject(hall, "link_degraded", cable="CBL-12-01-00", speed_gbps="down")
    res = run_check(hall)
    assert [f.class_ for f in res.findings] == ["link_degraded"], describe(res)
