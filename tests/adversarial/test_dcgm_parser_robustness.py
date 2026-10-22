# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""DCGM parsing: one malformed label must not blank a node; duplicate series must not hide an XID.

1. `parse_prometheus` decodes every label value with `unicode_escape`. A single invalid escape in any
   label of any line (even a metric family the check does not read) raises UnicodeDecodeError, the
   whole observation becomes "dcgm unreachable", and a GPU with XID 79 on that node is not reported
   (exit 0).
2. `parse_dcgm` keeps the last sample per (gpu, metric). When the exporter emits a health counter more
   than once for a GPU (per MIG instance, or a second exporter scrape merged by a proxy), a later 0
   overwrites an earlier 79: the unhealthy GPU is missed.
"""

from __future__ import annotations

import re

import pytest
from adv_helpers import Hall, by_class, describe, inject, run_check, small_spec

pytestmark = pytest.mark.integration


def test_invalid_label_escape_does_not_hide_gpu_fault():
    hall = Hall.generate(small_spec())
    inject(hall, "gpu_unhealthy", node="node1", gpu=2)

    def bad(node: str, text: str) -> str:
        return text.replace('Hostname="node1"', r'Hostname="node1\x"', 1) if node == "node1" else text

    res = run_check(hall, dcgm_hook=bad)
    assert [(f.device, f.interface) for f in by_class(res, "gpu_unhealthy")] == [("node1", "gpu2")], (
        describe(res) + f"\nnotes={res.notes}"
    )


def test_duplicate_health_series_take_the_worst_value():
    hall = Hall.generate(small_spec())
    inject(hall, "gpu_unhealthy", node="node1", gpu=2)

    def mig(node: str, text: str) -> str:
        out = []
        for line in text.splitlines():
            if node == "node1" and line.startswith("DCGM_FI_DEV_XID_ERRORS") and 'gpu="2"' in line:
                out.append(line.replace('gpu="2"', 'gpu="2",GPU_I_ID="0"'))
                out.append(re.sub(r"\s\S+$", " 0", line.replace('gpu="2"', 'gpu="2",GPU_I_ID="1"')))
            else:
                out.append(line)
        return "\n".join(out) + "\n"

    res = run_check(hall, dcgm_hook=mig)
    assert [(f.device, f.interface) for f in by_class(res, "gpu_unhealthy")] == [("node1", "gpu2")], describe(
        res
    )
