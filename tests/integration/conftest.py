# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the integration tier. Every fixture skips cleanly when simhall is not importable."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from simhall_helpers import SimServer, cli_env, start_server


@pytest.fixture(scope="session")
def golden_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimServer]:
    """``simhall serve --golden hall-b`` for the whole session."""
    pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)
    workdir = tmp_path_factory.mktemp("simhall-golden")
    server = start_server(["--golden", "hall-b"], workdir)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def golden(golden_server: SimServer) -> Iterator[SimServer]:
    """The golden hall in its pristine state (one wrong_port fault) before and after each test."""
    golden_server.reset()
    yield golden_server
    golden_server.reset()


@pytest.fixture
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Environment for running the CLI as a subprocess with an isolated cwd and config dir."""
    monkeypatch.chdir(tmp_path)
    return cli_env(tmp_path)
