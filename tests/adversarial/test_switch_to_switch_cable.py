# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A switch-to-switch cable in intent (MLAG peer link, leaf-spine uplink) is intended at both ends.

`diff` registers only one switch end of a cable in `intended_at` (the b end when b is a switch). The
other end's LLDP row (leaf3/20 sees leaf4 swp20) then becomes an `unexpected_link` "with no cable in
intent", although intent has exactly that cable. Informational, but a false statement in the report,
and every uplink of a hall with spines in NetBox produces one.
"""

from __future__ import annotations

import copy

import pytest
from adv_helpers import Hall, by_class, describe, run_check, small_spec

pytestmark = pytest.mark.integration


def _peer_link(sw: str, table: dict) -> dict:
    table = copy.deepcopy(table)
    other = "leaf4" if sw == "leaf3" else "leaf3"
    table["swp20"]["link"]["state"] = {"up": {}}
    table["swp20"]["lldp"] = {
        "neighbor": {
            other: {
                "chassis": {"chassis-id": f"02:1e:af:00:00:0{other[-1]}", "system-name": other},
                "port": {"id": "swp20", "id-type": "if_name"},
            }
        }
    }
    return table


def test_peer_link_cabled_as_intended_has_no_findings():
    hall = Hall.generate(small_spec())
    intent = hall.intent()
    intent["cables"].append(
        {
            "label": "PEER-1",
            "a": {"device": "leaf3", "interface": "swp20"},
            "b": {"device": "leaf4", "interface": "swp20"},
            "speed_gbps": 400,
        }
    )
    res = run_check(hall, intent=intent, lldp_hook=_peer_link)
    assert by_class(res, "unexpected_link") == [], describe(res)
    assert res.summary.links_ok == res.summary.links_total
