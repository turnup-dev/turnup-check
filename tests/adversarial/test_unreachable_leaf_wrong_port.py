# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A NIC seen on a reachable leaf when intent puts it on an unreachable leaf is a wrong_port.

leaf3's management is down (HTTP 500), and node1 mlx5_0, intended for leaf3/1, shows up on leaf4/9.
The diff skips every cable whose intended switch is unreadable, so the sighting on leaf4 is left to
the `unexpected_link` pass and reported as informational ("no cable in intent"): exit code and fix
ignore a move the check can see. CONTRACT 4: "a NIC observed on port P' of switch S' when intent says
port P of switch S becomes one wrong_port finding" holds whether or not S itself answered.
"""

from __future__ import annotations

import pytest
from adv_helpers import Hall, by_class, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def test_sighting_on_other_leaf_is_wrong_port_even_if_intended_leaf_is_down():
    hall = Hall.generate(small_spec())
    inject(hall, "unreachable_node", node="leaf3", mode="http_500")
    inject(hall, "wrong_port", cable="CBL-12-01-00", to_device="leaf4", to_interface="swp9")
    res = run_check(hall)
    wrong = by_class(res, "wrong_port")
    assert [(f.cable, f.observed) for f in wrong] == [("CBL-12-01-00", "leaf4/9")], describe(res)
    assert wrong[0].fix == "move cable from leaf4/9 to leaf3/1"
    assert by_class(res, "unexpected_link") == [], describe(res)
