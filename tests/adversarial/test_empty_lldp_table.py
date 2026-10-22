# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A reachable switch that returns an empty LLDP/interface document is unreadable, not 'all unplugged'.

A leaf always has interfaces; an eAPI result with no `interfaceStatuses` (or an NVUE `{}` document)
means the collector got nothing usable (wrong VRF/command filter, a proxy answering 200, an empty
NVUE revision). Today the diff reports `missing_link` with "seat cable" for every cable on that leaf,
and with `lldpNeighbors` present but `interfaceStatuses` empty it reports `link_degraded` for every
cable. Expected: the switch is reported once (unreachable_node / lldp unreadable) and its links are
"not verified", exactly like a switch that timed out.
"""

from __future__ import annotations

import pytest
from adv_helpers import Hall, by_class, describe, run_check, small_spec

pytestmark = pytest.mark.integration


def test_empty_eapi_result_is_unreadable_not_missing_links():
    hall = Hall.generate(small_spec(switch_vendor="arista"))
    res = run_check(hall, lldp_hook=lambda sw, t: {} if sw == "leaf3" else t)
    assert by_class(res, "missing_link") == [], describe(res)
    assert [f.device for f in by_class(res, "unreachable_node")] == ["leaf3"], describe(res)


def test_eapi_without_interface_statuses_is_not_eight_degraded_links():
    hall = Hall.generate(small_spec(switch_vendor="arista"))

    def no_status(sw: str, t: dict) -> dict:
        return {"lldpNeighbors": t["lldpNeighbors"], "interfaceStatuses": {}} if sw == "leaf3" else t

    res = run_check(hall, lldp_hook=no_status)
    assert by_class(res, "link_degraded") == [], describe(res)


def test_empty_nvue_document_is_unreadable_not_missing_links():
    hall = Hall.generate(small_spec())
    res = run_check(hall, lldp_hook=lambda sw, t: {} if sw == "leaf3" else t)
    assert by_class(res, "missing_link") == [], describe(res)
