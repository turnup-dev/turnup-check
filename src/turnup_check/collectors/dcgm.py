# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""DCGM-exporter parser (Prometheus text exposition, public metric names), CONTRACT 3.3."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from turnup_check.models import DcgmObservation

METRICS: tuple[str, ...] = (
    "DCGM_FI_DEV_GPU_TEMP",
    "DCGM_FI_DEV_POWER_USAGE",
    "DCGM_FI_DEV_SM_CLOCK",
    "DCGM_FI_DEV_FB_TOTAL",
    "DCGM_FI_DEV_XID_ERRORS",
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
    "DCGM_FI_DEV_ROW_REMAP_FAILURE",
    "DCGM_FI_DEV_PCIE_REPLAY_COUNTER",
    "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
    "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
    "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
)
UNHEALTHY_IF_POSITIVE: tuple[str, ...] = (
    "DCGM_FI_DEV_XID_ERRORS",
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
    "DCGM_FI_DEV_ROW_REMAP_FAILURE",
    "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
)
NVLINK_CRC_LIMIT = 100

# Repeated samples of one family for one GPU (per MIG instance, merged scrapes) are aggregated
# toward the worst value: the largest error counter, the smallest NVLink bandwidth.
WORST_IS_MAX: frozenset[str] = frozenset(
    (*UNHEALTHY_IF_POSITIVE, "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL")
)
WORST_IS_MIN: frozenset[str] = frozenset({"DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"})

_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL_NAME = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}

Sample = tuple[str, dict[str, str], float]


def _parse_labels(line: str, i: int) -> tuple[dict[str, str], int] | None:
    """Parse ``{k="v",...}`` starting at ``line[i] == "{"``; values may hold ``}``, ``,`` and escapes.

    Only the three escapes of the Prometheus text format are decoded (``\\\\``, ``\\"``, ``\\n``); any
    other backslash is kept literally. Returns ``(labels, index after "}")`` or ``None`` when malformed.
    """
    labels: dict[str, str] = {}
    n = len(line)
    i += 1
    while True:
        while i < n and line[i] in " ,":
            i += 1
        if i >= n:
            return None
        if line[i] == "}":
            return labels, i + 1
        m = _LABEL_NAME.match(line, i)
        if not m:
            return None
        key, i = m.group(1), m.end()
        buf: list[str] = []
        while i < n and line[i] != '"':
            if line[i] == "\\" and i + 1 < n:
                nxt = line[i + 1]
                buf.append(_ESCAPES.get(nxt, "\\" + nxt))
                i += 2
            else:
                buf.append(line[i])
                i += 1
        if i >= n:
            return None
        labels[key] = "".join(buf)
        i += 1


def _parse_line(line: str) -> Sample | None:
    m = _NAME.match(line)
    if not m:
        return None
    name, i = m.group(0), m.end()
    labels: dict[str, str] = {}
    if i < len(line) and line[i] == "{":
        parsed = _parse_labels(line, i)
        if parsed is None:
            return None
        labels, i = parsed
    rest = line[i:].split()
    if not rest or len(rest) > 2 or (i < len(line) and not line[i].isspace()):
        return None
    try:
        return name, labels, float(rest[0])
    except ValueError:
        return None


def parse_prometheus(text: str) -> Iterable[Sample]:
    """Yield ``(metric, labels, value)`` for every sample line; comments and malformed lines are skipped.

    One bad line (an odd escape, an unbalanced quote) never fails the rest of the document.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sample = _parse_line(line)
        if sample is not None:
            yield sample


def _merge(name: str, old: float | None, new: float) -> float:
    if old is None or math.isnan(old):
        return new
    if math.isnan(new):
        return old
    if name in WORST_IS_MAX:
        return max(old, new)
    if name in WORST_IS_MIN:
        return min(old, new)
    return new


def _gpu_index(labels: dict[str, str]) -> int | None:
    g = labels.get("gpu")
    if g is None:
        return None
    try:
        return int(g)
    except ValueError:
        return None


def parse_dcgm(text: str) -> DcgmObservation:
    """DCGM-exporter ``/metrics`` text -> DcgmObservation (reachable=True)."""
    per_gpu: dict[int, dict[str, float]] = {}
    temp_gpus: set[int] = set()
    for name, labels, value in parse_prometheus(text):
        if name not in METRICS:
            continue
        gpu = _gpu_index(labels)
        if gpu is None:
            continue
        metrics = per_gpu.setdefault(gpu, {})
        metrics[name] = _merge(name, metrics.get(name), value)
        if name == "DCGM_FI_DEV_GPU_TEMP":
            temp_gpus.add(gpu)
    gpu_count = len(temp_gpus) if temp_gpus else len(per_gpu)
    unhealthy = sorted(
        gpu for gpu, metrics in per_gpu.items() if any(metrics.get(m, 0) > 0 for m in UNHEALTHY_IF_POSITIVE)
    )
    nvlink_degraded = False
    if gpu_count > 1:
        for metrics in per_gpu.values():
            if metrics.get("DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL", 0) > NVLINK_CRC_LIMIT:
                nvlink_degraded = True
            if (
                "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL" in metrics
                and metrics["DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"] == 0
            ):
                nvlink_degraded = True
    raw = {str(g): {k: v for k, v in m.items()} for g, m in sorted(per_gpu.items())}
    return DcgmObservation(
        reachable=True,
        gpu_count=gpu_count,
        unhealthy_gpus=unhealthy,
        nvlink_degraded=nvlink_degraded,
        raw=raw,
    )


__all__ = ["METRICS", "NVLINK_CRC_LIMIT", "UNHEALTHY_IF_POSITIVE", "parse_dcgm", "parse_prometheus"]
