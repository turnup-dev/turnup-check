# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""The check as a library call: poll, diff, fetch NIC port evidence where it matters, diff again.

Other packages (the Acceptance verifier) should call ``check`` rather than ``poll_all`` + ``diff``
directly: the first Redfish pass skips the NIC port branch (the bulk of the requests), so a single
``poll_all`` gives link findings without node-side evidence and never the ``check NIC ... on ...`` fix.
"""

from __future__ import annotations

from collections.abc import Iterable

from turnup_check.collectors import DEFAULT_TIMEOUT, Collector, normalize_only, poll_all, poll_ports
from turnup_check.diff import DiffResult, diff, nodes_needing_ports
from turnup_check.models import Creds, Intent, Observations


async def check(
    intent: Intent,
    collector: Collector,
    creds: Creds | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    only: Iterable[str] | str | None = None,
) -> tuple[Observations, DiffResult]:
    """Run one complete check and return the observations and the diff result.

    Pass one: every selected source, Redfish without the Chassis/NetworkAdapters/Ports branch.
    Pass two: port details only for reachable nodes that have a link finding (wrong_port, missing_link,
    link_degraded) or LLDP neighbors whose port could not be resolved (MAC ids); then the diff is
    recomputed with the node-side evidence (NIC link state, LLDP as the NIC sees it, NIC MACs for
    MAC-advertising neighbors).
    """
    creds = creds or Creds()
    polled = normalize_only(only)
    obs = await poll_all(intent, collector, creds, timeout=timeout, only=polled)
    result = diff(intent, obs)
    needs = nodes_needing_ports(intent, result) if "redfish" in polled else set()
    # A BMC that did not answer pass one would only cost another timeout (CONTRACT 9.4).
    needs = {n for n in needs if (rf := obs.redfish.get(n)) is not None and rf.reachable}
    if needs:
        updated = await poll_ports(intent, collector, creds, timeout=timeout, nodes=needs, obs=obs)
        if updated:
            result = diff(intent, obs)
    return obs, result


__all__ = ["check"]
