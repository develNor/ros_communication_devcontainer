from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_e2e = pytest.mark.skip(reason="set ROSOTACOM_RUN_E2E=1 to run Docker E2E tests")
    run_e2e = os.environ.get("ROSOTACOM_RUN_E2E") == "1"

    for item in items:
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)
