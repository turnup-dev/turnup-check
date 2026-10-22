# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""poll_all: concurrency, isolation of failures, source selection, and the HttpCollector semaphore."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from helpers import FakeCollector

from turnup_check.collectors import (
    MAX_CONCURRENCY,
    PER_HOST_CONCURRENCY,
    CollectorError,
    HttpCollector,
    normalize_only,
    poll_all,
    poll_ports,
)
from turnup_check.models import Creds, Device, Intent, Mgmt


async def test_poll_all_runs_every_device_concurrently(mini_intent, fake):
    fake.delay = 0.05
    started = asyncio.get_running_loop().time()
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.5  # 6 calls of 50 ms ran together, not one after another
    assert fake.max_in_flight == 6
    assert (
        set(obs.lldp) == {"leaf3", "leaf4"}
        and set(obs.redfish) == {"node7", "node8"}
        and set(obs.dcgm) == {"node7", "node8"}
    )
    assert obs.lldp_protocols == {"leaf3": "nvue", "leaf4": "nvue"}
    assert obs.polled == ["lldp", "redfish", "dcgm"]
    assert obs.any_reachable() is True


async def test_collector_exceptions_are_isolated(mini_intent):
    class Broken(FakeCollector):
        async def lldp(self, switch, creds, timeout):
            if switch.name == "leaf3":
                raise RuntimeError("bug")
            raise CollectorError("nvue: HTTP 500")

        async def redfish(self, node, creds, timeout):
            raise ValueError("boom")

        async def dcgm(self, node, timeout):
            raise OSError("down")

    obs = await poll_all(mini_intent, Broken.from_intent(mini_intent), timeout=1.0)
    assert obs.lldp == {}
    assert obs.lldp_errors == {"leaf3": "RuntimeError: bug", "leaf4": "nvue: HTTP 500"}
    assert all(not o.reachable for o in obs.redfish.values())
    assert all(not o.reachable for o in obs.dcgm.values())
    assert obs.any_reachable() is False
    assert obs.sources() == [
        {"kind": "lldp", "protocol": "nvue", "devices": 2},
        {"kind": "redfish", "devices": 2},
        {"kind": "dcgm", "devices": 2},
    ]


async def test_only_selects_sources(mini_intent, fake):
    obs = await poll_all(mini_intent, fake, timeout=1.0, only="dcgm,lldp")
    assert obs.polled == ["lldp", "dcgm"]
    assert obs.redfish == {} and len(obs.dcgm) == 2
    assert {k for k, _ in fake.calls} == {"lldp", "dcgm"}
    assert obs.sources() == [
        {"kind": "lldp", "protocol": "nvue", "devices": 2},
        {"kind": "dcgm", "devices": 2},
    ]


def test_normalize_only():
    assert normalize_only(None) == ["lldp", "redfish", "dcgm"]
    assert normalize_only("dcgm, lldp") == ["lldp", "dcgm"]
    assert normalize_only(["redfish"]) == ["redfish"]
    assert normalize_only("") == ["lldp", "redfish", "dcgm"]
    with pytest.raises(ValueError, match="unknown source"):
        normalize_only("snmp")


def _counting_handler():
    state = {"in_flight": 0, "peak": 0, "per_host_peak": {}}
    live: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.netloc.decode()
        state["in_flight"] += 1
        live[host] = live.get(host, 0) + 1
        state["peak"] = max(state["peak"], state["in_flight"])
        state["per_host_peak"][host] = max(state["per_host_peak"].get(host, 0), live[host])
        await asyncio.sleep(0.005)
        state["in_flight"] -= 1
        live[host] -= 1
        return httpx.Response(200, text="")

    return handler, state


async def test_http_collector_bounds_in_flight_requests_per_host():
    handler, state = _counting_handler()
    nodes = [
        Device(name=f"n{i}", role="gpu-node", mgmt=Mgmt(dcgm_url=f"http://127.0.0.1:1/dev/n{i}"))
        for i in range(150)
    ]
    intent = Intent(site="s", devices=nodes)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client, per_host=4) as c,
    ):
        obs = await poll_all(intent, c, Creds(), timeout=1.0, only="dcgm")
    assert len(obs.dcgm) == 150 and all(o.reachable for o in obs.dcgm.values())
    assert state["peak"] <= 4  # one host (a simulator) never sees more than per_host live requests at once


async def test_dead_devices_behind_one_host_do_not_queue_the_live_ones():
    """CONTRACT 9.4: 12 timing-out devices behind one host:port cost about one timeout, not 12 / 4."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/dev/dead" in request.url.path:
            await asyncio.sleep(30)
        await asyncio.sleep(0.005)
        return httpx.Response(200, text='DCGM_FI_DEV_GPU_TEMP{gpu="0"} 40\n')

    nodes = [
        Device(name=f"dead{i}", role="gpu-node", mgmt=Mgmt(dcgm_url=f"http://127.0.0.1:1/dev/dead{i}"))
        for i in range(12)
    ] + [
        Device(name=f"live{i}", role="gpu-node", mgmt=Mgmt(dcgm_url=f"http://127.0.0.1:1/dev/live{i}"))
        for i in range(40)
    ]
    timeout = 0.5
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client, per_host=4) as c,
    ):
        t0 = time.monotonic()
        obs = await poll_all(Intent(site="s", devices=nodes), c, Creds(), timeout=timeout, only="dcgm")
        elapsed = time.monotonic() - t0
    assert sorted(n for n, o in obs.dcgm.items() if not o.reachable) == sorted(f"dead{i}" for i in range(12))
    assert all(obs.dcgm[f"live{i}"].reachable for i in range(40))
    assert elapsed <= 2 * timeout + 0.5, elapsed


async def test_http_collector_global_bound_across_many_hosts():
    handler, state = _counting_handler()
    nodes = [
        Device(name=f"n{i}", role="gpu-node", mgmt=Mgmt(dcgm_url=f"http://10.0.{i // 250}.{i % 250}:9400"))
        for i in range(150)
    ]
    intent = Intent(site="s", devices=nodes)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        obs = await poll_all(intent, c, Creds(), timeout=1.0, only="dcgm")
    assert len(obs.dcgm) == 150
    assert 4 < state["peak"] <= MAX_CONCURRENCY  # real halls: one request per host, up to 64 in flight
    assert max(state["per_host_peak"].values()) <= 4


def test_per_host_default_from_env(monkeypatch):
    monkeypatch.setenv("TURNUP_PER_HOST", "9")
    assert HttpCollector().per_host == 9
    monkeypatch.setenv("TURNUP_PER_HOST", "junk")
    assert HttpCollector().per_host == PER_HOST_CONCURRENCY
    monkeypatch.delenv("TURNUP_PER_HOST")
    assert HttpCollector(per_host=0).per_host == 1


async def test_poll_ports_second_pass_only_touches_named_nodes(mini_intent, fake):
    fake.strict_ports = True
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    assert all(o.ports == [] for o in obs.redfish.values())
    updated = await poll_ports(mini_intent, fake, None, timeout=1.0, nodes={"node7"}, obs=obs)
    assert updated == ["node7"]
    assert len(obs.redfish["node7"].ports) == 4 and obs.redfish["node8"].ports == []
    assert fake.calls.count(("redfish+ports", "node7")) == 1
    assert ("redfish+ports", "node8") not in fake.calls


async def test_poll_ports_keeps_first_pass_when_second_fails(mini_intent, fake):
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    fake.node_unreachable("node7", "flaky")
    updated = await poll_ports(mini_intent, fake, None, timeout=1.0, nodes={"node7"}, obs=obs)
    assert updated == [] and obs.redfish["node7"].reachable is True


async def test_poll_all_ports_for_passes_want_ports(mini_intent, fake):
    fake.strict_ports = True
    obs = await poll_all(mini_intent, fake, timeout=1.0, ports_for={"node8"})
    assert obs.redfish["node7"].ports == [] and len(obs.redfish["node8"].ports) == 4


async def test_redfish_call_falls_back_for_collectors_without_want_ports(mini_intent):
    class OldStyle(FakeCollector):
        async def redfish(self, node, creds, timeout):  # no want_ports keyword
            return await super().redfish(node, creds, timeout, want_ports=True)

    obs = await poll_all(mini_intent, OldStyle.from_intent(mini_intent), timeout=1.0, ports_for={"node7"})
    assert len(obs.redfish["node7"].ports) == 4


async def test_http_collector_manages_its_own_client():
    async with HttpCollector() as c:
        assert isinstance(c.client, httpx.AsyncClient)
    with pytest.raises(RuntimeError):
        _ = HttpCollector().client


async def test_engine_check_resolves_mac_neighbors_through_second_pass(mini_intent, fake):
    """MAC-advertising NICs with no MACs in intent: pass one sees missing links, pass two fetches the NIC MACs
    from Redfish for exactly those nodes and the re-diff resolves every link."""
    from turnup_check.engine import check

    fake.strict_ports = True
    fake.mac_mode(mini_intent, "leaf4")
    for dev in mini_intent.devices:
        for itf in dev.interfaces:
            itf.mac = None
    obs, result = await check(mini_intent, fake, Creds(), timeout=1.0)
    assert result.findings == [], [f.model_dump(by_alias=True) for f in result.findings]
    assert result.summary.links_ok == 8
    assert sorted(n for k, n in fake.calls if k == "redfish+ports") == [
        "node7",
        "node8",
    ]  # both have leaf4 links
    assert len(obs.redfish["node7"].ports) == 4


async def test_engine_check_skips_second_pass_without_redfish(mini_intent, fake):
    from turnup_check.engine import check

    fake.strict_ports = True
    fake.unseat(mini_intent, "CBL-12-07-03")
    _obs, result = await check(mini_intent, fake, Creds(), timeout=1.0, only="lldp")
    assert [f.class_ for f in result.findings] == ["missing_link"]
    assert not [c for c in fake.calls if c[0].startswith("redfish")]
