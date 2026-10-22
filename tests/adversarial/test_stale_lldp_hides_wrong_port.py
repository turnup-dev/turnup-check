# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A stale (aging, link-down) LLDP entry on the intended port must not hide a live wrong_port.

Switches keep an LLDP neighbor until its TTL (120 s) runs out; simhall models this for
`link_degraded(cable, "down")` ("link down, LLDP neighbor still present (aging entry)"). A technician
who moves a cable from leaf3/1 to leaf3/9 inside the TTL leaves the old entry on leaf3/1 (port down)
while the NIC is live on leaf3/9. The diff takes the first resolved row at the intended port as a match
and reports `link_degraded` ("check cable at leaf3/1") plus an informational `unexpected_link` at
leaf3/9: the move is never reported, and the exit code is driven by the wrong finding.
"""

from __future__ import annotations

import copy

import pytest
from adv_helpers import Hall, classes, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def _stale_on_intended(switch: str, table: dict) -> dict:
    if switch != "leaf3":
        return table
    table = copy.deepcopy(table)
    table["swp1"]["lldp"] = copy.deepcopy(table["swp9"]["lldp"])  # old entry, port swp1 is down
    return table


def test_live_sighting_beats_stale_entry_on_intended_port():
    hall = Hall.generate(small_spec())
    inject(hall, "wrong_port", cable="CBL-12-01-00", to_device="leaf3", to_interface="swp9")
    res = run_check(hall, lldp_hook=_stale_on_intended)
    assert classes(res) == ["wrong_port"], describe(res)
    assert res.findings[0].observed == "leaf3/9"
    assert res.findings[0].fix == "move cable from leaf3/9 to leaf3/1"
