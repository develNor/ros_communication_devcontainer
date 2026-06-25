python := ".venv/bin/python"
bin := ".venv/bin"

default:
	@just --list

venv:
	python3 -m venv .venv
	{{python}} -m pip install --upgrade pip

setup: venv
	{{python}} -m pip install -e ".[dev,plots]"
	{{python}} -m pre_commit install

format:
	{{python}} -m ruff check --fix .
	{{python}} -m ruff format .

lint:
	{{python}} -m ruff check .
	{{python}} -m ruff format --check .
	{{python}} -m py_compile src/rosotacom/*.py run_session_in_container.py stop_session_in_container.py

typecheck:
	{{python}} -m mypy

test: test-unit test-contract

test-unit:
	{{python}} -m pytest -q tests/unit

test-contract:
	{{python}} -m pytest -q tests/contract

coverage: test-nondocker-cov

test-nondocker-cov:
	{{python}} -m pytest -q tests/unit tests/contract --cov=rosotacom --cov-report=term-missing --cov-report=xml:coverage.xml

test-e2e-smoke:
	ROSOTACOM_RUN_E2E=1 {{python}} -m pytest -q tests/e2e/ -m e2e

test-e2e-fast: test-e2e-smoke

docs:
	{{python}} -m pytest -q tests/contract/test_markdown_links.py tests/contract/test_readme_examples.py tests/contract/test_pytest_policy.py

package: _package-build _package-smoke

_package-build:
	rm -rf build dist *.egg-info src/*.egg-info
	{{python}} -m build
	{{python}} -m twine check dist/*
	{{bin}}/check-wheel-contents dist/*.whl

_package-smoke:
	#!/usr/bin/env bash
	set -euo pipefail

	tmpdir="$(mktemp -d)"
	trap 'rm -rf "$tmpdir"' EXIT

	"{{python}}" -m venv "$tmpdir/venv"
	"$tmpdir/venv/bin/python" -m pip install dist/*.whl
	cd "$tmpdir"
	"$tmpdir/venv/bin/rosotacom" --version
	"$tmpdir/venv/bin/python" -m rosotacom --version
	"$tmpdir/venv/bin/rosotacom" examples create "$tmpdir/examples"
	"$tmpdir/venv/bin/rosotacom" list-sessions --rosotacom-config "$tmpdir/examples/rosotacom.yaml"
	"$tmpdir/venv/bin/rosotacom" scenario list --rosotacom-config "$tmpdir/examples/rosotacom.yaml"
	"$tmpdir/venv/bin/python" - <<'PY'
	from importlib import resources

	expected = (
	    "py.typed",
	    "resources/ros2docker.json.example",
	    "resources/examples/rosotacom.yaml",
	    "resources/examples/sessions/1_heartbeat/session-definition.yaml",
	    "resources/examples/scenarios/2_native_chatter/scenario-definition.yaml",
	    "resources/ws/session/creation/run_session.py",
	    "resources/ws/ros2src/com_msgs/msg/EchoHeartbeat.msg",
	)
	missing = [
	    path
	    for path in expected
	    if not resources.files("rosotacom").joinpath(path).is_file()
	]
	if missing:
	    raise SystemExit(f"missing packaged resources: {', '.join(missing)}")
	PY

pre-commit:
	{{python}} -m pre_commit run --all-files

check: lint typecheck test-nondocker-cov docs package
