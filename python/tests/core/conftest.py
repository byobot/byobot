import os

import pytest
from hypothesis import HealthCheck, settings

from .driver import Unsupported
from .drivers.v0 import V0
from .harness import World
from .model import Reference

DRIVERS = {"model": Reference, "v0": V0}

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
