# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""DCGM-exporter parser and the DCGM collector path."""

from __future__ import annotations

import httpx
from helpers import load_text

from turnup_check.collectors import HttpCollector
from turnup_check.collectors.dcgm import parse_dcgm, parse_prometheus
from turnup_check.models import Device, Mgmt


def test_healthy_four_gpus():
    obs = parse_dcgm(load_text("dcgm/healthy.txt"))
    assert obs.reachable is True
    assert obs.gpu_count == 4
    assert obs.unhealthy_gpus == []
    assert obs.nvlink_degraded is False
    assert obs.raw["2"]["DCGM_FI_DEV_GPU_TEMP"] == 43.0


def test_xid_marks_one_gpu_unhealthy():
    obs = parse_dcgm(load_text("dcgm/xid.txt"))
    assert obs.unhealthy_gpus == [2]
    assert obs.raw["2"]["DCGM_FI_DEV_XID_ERRORS"] == 79.0


def test_missing_gpu_count():
    assert parse_dcgm(load_text("dcgm/missing_gpu.txt")).gpu_count == 3


def test_nvlink_crc_and_zero_bandwidth():
    assert parse_dcgm(load_text("dcgm/nvlink_crc.txt")).nvlink_degraded is True
    assert parse_dcgm(load_text("dcgm/nvlink_zero_bw.txt")).nvlink_degraded is True


def test_single_gpu_zero_bandwidth_is_not_degraded():
    obs = parse_dcgm(load_text("dcgm/single_gpu_zero_bw.txt"))
    assert obs.gpu_count == 1 and obs.nvlink_degraded is False


def test_ecc_and_remap_failures():
    obs = parse_dcgm(load_text("dcgm/ecc_and_remap.txt"))
    assert obs.unhealthy_gpus == [0, 3]


def test_prometheus_parser_tolerates_noise():
    text = "\n".join(
        [
            "# HELP x y",
            'DCGM_FI_DEV_GPU_TEMP{gpu="0",Hostname="n\\"q"} 40 1700000000',
            "garbage line without value",
            'DCGM_FI_DEV_GPU_TEMP{gpu="1"} NaN',
            'DCGM_FI_DEV_GPU_TEMP{gpu="x"} 1',
            'OTHER_METRIC{gpu="5"} 1',
            "DCGM_FI_DEV_GPU_TEMP 12",
        ]
    )
    samples = list(parse_prometheus(text))
    assert samples[0] == ("DCGM_FI_DEV_GPU_TEMP", {"gpu": "0", "Hostname": 'n"q'}, 40.0)
    obs = parse_dcgm(text)
    assert obs.gpu_count == 2  # gpu 0 and 1 (NaN still names a GPU); "x" and unlabeled samples ignored


def test_empty_text_is_zero_gpus():
    obs = parse_dcgm("")
    assert obs.reachable is True and obs.gpu_count == 0 and obs.unhealthy_gpus == []


async def test_http_collector_dcgm_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dev/node7/metrics":
            return httpx.Response(200, text=load_text("dcgm/healthy.txt"))
        if request.url.path == "/dev/down/metrics":
            return httpx.Response(503, text="down")
        raise httpx.ConnectError("refused", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        ok = await c.dcgm(
            Device(name="node7", role="gpu-node", mgmt=Mgmt(dcgm_url="http://127.0.0.1:8900/dev/node7")), 1.0
        )
        down = await c.dcgm(
            Device(name="d", role="gpu-node", mgmt=Mgmt(dcgm_url="http://127.0.0.1:8900/dev/down")), 1.0
        )
        refused = await c.dcgm(
            Device(name="r", role="gpu-node", mgmt=Mgmt(dcgm_url="http://127.0.0.1:1/dev/r")), 1.0
        )
        no_url = await c.dcgm(Device(name="n", role="gpu-node"), 1.0)
    assert ok.reachable is True and ok.gpu_count == 4
    assert down.reachable is False and down.raw_error == "HTTP 503"
    assert refused.reachable is False and "ConnectError" in refused.raw_error
    assert no_url.reachable is False and "no dcgm_url" in no_url.raw_error


def test_label_values_with_braces_commas_and_odd_escapes():
    text = (
        'DCGM_FI_DEV_GPU_TEMP{gpu="0",modelName="NVIDIA {GB300}, rev A",Hostname="node1\\x"} 40\n'
        'DCGM_FI_DEV_GPU_TEMP{gpu="1",modelName="a\\"b}c\\\\",UUID="GPU-1"} 41\n'
        'DCGM_FI_DEV_XID_ERRORS{gpu="1",modelName="x}"} 79\n'
        'DCGM_FI_DEV_XID_ERRORS{gpu="1",GPU_I_ID="1"} 0\n'
        'DCGM_FI_DEV_GPU_TEMP{gpu="2",modelName="unterminated} 1\n'
    )
    samples = list(parse_prometheus(text))
    assert samples[0][1]["modelName"] == "NVIDIA {GB300}, rev A"
    assert samples[0][1]["Hostname"] == "node1\\x"  # unknown escape kept literally
    assert samples[1][1]["modelName"] == 'a"b}c\\'
    assert len(samples) == 4  # the malformed line is skipped, nothing else
    obs = parse_dcgm(text)
    assert obs.gpu_count == 2 and obs.unhealthy_gpus == [1]  # worst XID across series wins
