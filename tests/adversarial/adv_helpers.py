# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the adversarial tier: an in-process collector over a simhall ``Hall``.

Every adversarial test builds a hall with simhall's Python API, injects faults, runs the same
two-pass ``engine.check`` the CLI runs, and asserts what CONTRACT.md section 4 says the report should
contain. Tests that pin a known defect are ``xfail(strict=True)`` so they flip to XPASS (and fail the
suite) the moment the defect is fixed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)

from simhall import Fault, Hall, HallSpec

from turnup_check.collectors import CollectorError
from turnup_check.collectors.dcgm import parse_dcgm
from turnup_check.collectors.lldp_eapi import parse_eapi
from turnup_check.collectors.lldp_nvue import parse_nvue
from turnup_check.collectors.redfish import DictFetcher, walk_redfish
from turnup_check.diff import DiffResult
from turnup_check.engine import check
from turnup_check.models import (
    Creds,
    DcgmObservation,
    Device,
    Finding,
    Intent,
    LldpNeighbor,
    RedfishObservation,
)


class HallCollector:
    """Collector protocol over a simhall ``Hall`` (no HTTP). Hooks let a test tamper with any payload."""

    def __init__(
        self,
        hall: Hall,
        *,
        lldp_hook: Callable[[str, Any], Any] | None = None,
        redfish_hook: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        dcgm_hook: Callable[[str, str], str] | None = None,
    ) -> None:
        self.hall = hall
        self.lldp_hook = lldp_hook
        self.redfish_hook = redfish_hook
        self.dcgm_hook = dcgm_hook

    async def redfish(
        self, node: Device, creds: Creds, timeout: float, *, want_ports: bool = False
    ) -> RedfishObservation:
        mode = self.hall.reach(node.name, "redfish")
        if mode != "ok":
            return RedfishObservation(reachable=False, raw_error=mode)
        tree = self.hall.redfish_tree(node.name)
        if self.redfish_hook:
            tree = self.redfish_hook(node.name, tree)
        return await walk_redfish(DictFetcher(tree), timeout=timeout, want_ports=want_ports)

    async def lldp(self, switch: Device, creds: Creds, timeout: float) -> list[LldpNeighbor]:
        mode = self.hall.reach(switch.name, "switch")
        if mode != "ok":
            raise CollectorError(mode)
        table = self.hall.lldp_table(switch.name)
        if self.lldp_hook:
            table = self.lldp_hook(switch.name, table)
        if switch.lldp_protocol == "eapi":
            return parse_eapi(table, switch.name)
        return parse_nvue(table, switch.name)

    async def dcgm(self, node: Device, timeout: float) -> DcgmObservation:
        mode = self.hall.reach(node.name, "dcgm")
        if mode != "ok":
            return DcgmObservation(reachable=False, raw_error=mode)
        text = self.hall.dcgm_metrics(node.name)
        if self.dcgm_hook:
            text = self.dcgm_hook(node.name, text)
        return parse_dcgm(text)


def small_spec(**over: Any) -> HallSpec:
    """1 rack, 4 nodes x 4 ports, 2 NVIDIA leaves: small enough to reason about every port."""
    kw: dict[str, Any] = dict(
        site="adv",
        racks=1,
        nodes_per_rack=4,
        ports_per_node=4,
        leaves_per_rack=2,
        switch_vendor="nvidia",
        lldp_port_id_mode="name",
        seed=1,
    )
    kw.update(over)
    return HallSpec(**kw)


def run_check(
    hall: Hall,
    intent: Intent | dict[str, Any] | None = None,
    *,
    timeout: float = 3.0,
    **hooks: Any,
) -> DiffResult:
    """``engine.check`` over the hall; ``intent`` overrides the hall's own intent (to corrupt it)."""
    doc = intent if intent is not None else hall.intent()
    it = doc if isinstance(doc, Intent) else Intent.model_validate(doc)
    _, result = asyncio.run(check(it, HallCollector(hall, **hooks), Creds(), timeout=timeout))
    return result


def inject(hall: Hall, kind: str, **params: Any) -> Fault:
    return hall.inject(Fault(kind=kind, **params))


def classes(result: DiffResult) -> list[str]:
    return sorted(f.class_ for f in result.findings)


def by_class(result: DiffResult, cls: str) -> list[Finding]:
    return [f for f in result.findings if f.class_ == cls]


def cable(hall: Hall, label: str) -> dict[str, Any]:
    """The intent cable record for ``label``."""
    return next(c for c in hall.intent()["cables"] if c["label"] == label)


def describe(result: DiffResult) -> str:
    """One line per finding, for assertion messages."""
    return "\n".join(
        f"{f.class_:22} cable={f.cable} dev={f.device} if={f.interface} exp={f.expected} "
        f"obs={f.observed} fix={f.fix!r}"
        for f in result.findings
    )
