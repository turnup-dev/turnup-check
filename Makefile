# SPDX-License-Identifier: Apache-2.0
# turnup-check: activate your virtualenv first (python3 -m venv .venv && source .venv/bin/activate)
PY ?= python
SIMHALL_URL ?= http://127.0.0.1:8900/netbox

.PHONY: install test test-unit test-integration lint fmt run clean

install:
	pip install -q -e '.[dev,snmp]'

test:
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest -q -m "not integration"

test-integration:
	$(PY) -m pytest -q -m integration

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .
	ruff check --fix .

# The README's virtual-hall command, against a running `simhall serve --golden hall-b`.
run:
	turnup-check --site hall-b --intent $(SIMHALL_URL)

clean:
	rm -rf .pytest_cache .ruff_cache .hypothesis build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
