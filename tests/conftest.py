# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""pytest fixtures for the unit tier; the heavy lifting lives in ``helpers.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FIXTURES, FakeCollector

from turnup_check.intent.file import load_intent_file
from turnup_check.models import Intent


@pytest.fixture
def mini_intent() -> Intent:
    return load_intent_file(FIXTURES / "intent" / "hall-mini.yaml")


@pytest.fixture
def mini_intent_path() -> Path:
    return FIXTURES / "intent" / "hall-mini.yaml"


@pytest.fixture
def fake(mini_intent: Intent) -> FakeCollector:
    return FakeCollector.from_intent(mini_intent)
