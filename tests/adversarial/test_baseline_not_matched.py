# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""A node whose model has no baseline entry must not be reported as "firmware baseline ok" silently.

`Intent.baseline_for` matches the device model to a `firmware_baseline` key exactly. A baseline file
keyed "gb300 nvl72 compute tray" (or a NetBox device type renamed "GB300 NVL72 Compute Tray") matches
nothing: no component is compared, no note is written, `summary.firmware_ok` stays true and the CLI
prints "firmware baseline ok" over a node that is off baseline.
"""

from __future__ import annotations

import pytest
from adv_helpers import Hall, by_class, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def test_unmatched_baseline_model_is_compared_or_noted():
    hall = Hall.generate(small_spec())
    intent = hall.intent()
    model = next(iter(intent["firmware_baseline"]))
    intent["firmware_baseline"] = {model.lower(): intent["firmware_baseline"][model]}
    inject(hall, "firmware_off_baseline", node="node1", component="bios", version="0.0.1")
    res = run_check(hall, intent=intent)
    compared = bool(by_class(res, "firmware_off_baseline"))
    noted = any("node1" in n and "baseline" in n for n in res.notes)
    assert compared or noted, describe(res) + f"\nnotes={res.notes}"
    assert not (res.summary.firmware_ok and not compared), "summary claims firmware ok without comparing"
