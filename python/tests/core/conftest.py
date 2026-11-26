import os
import re
from collections import defaultdict

import pytest
from hypothesis import HealthCheck, settings

from .driver import Unsupported
from .drivers.cloud import Cloud
from .drivers.v1 import V1
from .harness import World
from .model import Reference

DRIVERS = {"model": Reference, "v1": V1, "cloud": Cloud}

_quiet = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]
settings.register_profile("ci", max_examples=60, derandomize=True, deadline=None, suppress_health_check=_quiet)
settings.register_profile("soak", max_examples=5000, deadline=None, suppress_health_check=_quiet)
settings.load_profile(os.environ.get("BYOBOT_HYPOTHESIS_PROFILE", "ci"))


def pytest_configure(config):
    config.addinivalue_line("markers", "baseline(**drivers): a driver's known failure, run as a strict xfail")


def pytest_collection_modifyitems(items):
    for item in items:
        marker = item.get_closest_marker("baseline")
        driver = getattr(item, "callspec", None) and item.callspec.params.get("driver")
        if marker and driver in marker.kwargs:
            item.add_marker(pytest.mark.xfail(strict=True, reason=f"{driver} baseline: {marker.kwargs[driver]}"))


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    try:
        return (yield)
    except Unsupported as exc:
        pytest.skip(f"no equivalent: {exc}")


@pytest.fixture(params=list(DRIVERS))
def driver(request):
    return request.param


@pytest.fixture
def world(driver, tmp_path):
    w = World(DRIVERS[driver](), tmp_path)
    yield w
    w.close()


_matrix: dict[str, dict[str, str]] = defaultdict(dict)


def pytest_runtest_logreport(report):
    found = re.search(r"\[(" + "|".join(DRIVERS) + r")[\]-]", report.nodeid)
    if not found or "tests/core/" not in report.nodeid.replace("\\", "/"):
        return
    if report.when != "call" and not report.skipped:
        return
    if hasattr(report, "wasxfail"):
        outcome = "xfail" if report.skipped else "XPASS"
    else:
        outcome = "no equivalent" if report.skipped else report.outcome
    row = re.sub(r"\[(" + "|".join(DRIVERS) + r")(-|\])", lambda m: "[" if m[2] == "-" else "", report.nodeid)
    _matrix[row.split("::", 1)[1]][found[1]] = outcome


def pytest_sessionfinish(session):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary or not _matrix:
        return
    lines = ["| scenario | " + " | ".join(DRIVERS) + " |", "|---" * (len(DRIVERS) + 1) + "|"]
    lines += [f"| {name} | " + " | ".join(row.get(d, "") for d in DRIVERS) + " |"
              for name, row in sorted(_matrix.items())]
    with open(summary, "a", encoding="utf-8") as out:
        out.write("\n".join(["### Core scenarios by driver", "", *lines, ""]))
