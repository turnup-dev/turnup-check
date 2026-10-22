# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A stale LLDP entry on a spare port, for a NIC that is live on its intended port, is not a finding.

A technician moves a cable from spare leaf3/9 to its intended leaf3/1 (the golden fix). For up to the
LLDP TTL the switch keeps the old neighbor on the now-down leaf3/9. The NIC is verified on leaf3/1;
the aged entry is noted, never reported as `unexpected_link` "with no cable in intent".
"""

from __future__ import annotations

import copy

import pytest
from adv_helpers import Hall, describe, run_check, small_spec

pytestmark = pytest.mark.integration


def _stale_on_spare(switch: str, table: dict) -> dict:
    if switch != "leaf3":
        return table
    table = copy.deepcopy(table)
    table["swp9"]["lldp"] = copy.deepcopy(table["swp1"]["lldp"])  # old entry, swp9 is down
    assert table["swp9"]["link"]["state"] == {"down": {}}
    return table


def test_stale_entry_on_down_spare_port_is_a_note():
    hall = Hall.generate(small_spec())
    res = run_check(hall, lldp_hook=_stale_on_spare)
    assert res.findings == [], describe(res)
    assert res.summary.links_ok == res.summary.links_total
    assert any("leaf3/9" in n and "stale" in n for n in res.notes), res.notes


def test_live_foreign_neighbor_on_spare_port_is_still_reported():
    """Control: an up spare port showing a node interface that is also on its intended port is a
    second cable (or a mis-patched one), so it stays an informational unexpected_link."""

    def live(switch: str, table: dict) -> dict:
        table = _stale_on_spare(switch, table)
        if switch == "leaf3":
            table["swp9"]["link"] = {"state": {"up": {}}, "speed": "400G"}
        return table

    res = run_check(Hall.generate(small_spec()), lldp_hook=live)
    assert [(f.class_, f.severity) for f in res.findings] == [("unexpected_link", "info")], describe(res)
