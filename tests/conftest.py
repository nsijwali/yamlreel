"""Shared test configuration.

`browser` marks the tests that need playwright and a Chromium build. Locally they skip when no browser is installed.
With YAMLREEL_REQUIRE_BROWSER=1 (the CI `browser` job) such a skip is a failure instead, so the job cannot pass
without actually running them.
"""
from __future__ import annotations

import os

import pytest

REQUIRE_BROWSER = os.environ.get("YAMLREEL_REQUIRE_BROWSER") == "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: needs playwright and Chromium; YAMLREEL_REQUIRE_BROWSER=1 makes a skip fail")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if REQUIRE_BROWSER and report.skipped and item.get_closest_marker("browser") is not None:
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = f"browser test skipped although YAMLREEL_REQUIRE_BROWSER=1: {reason}"
