"""S15: cost growth. One late event, one undo and one 30-set offline sync must not cost more as history grows.

Growth checks run on every PR at N and 2N authored events. Each cost is measured above the same operation on a
small history, so a fixed cost per write (macOS syncs to disk slowly) can't hide growth. With BYOBOT_SOAK set,
N is larger and the absolute budgets from the plan apply too.
"""

import os
import time
from datetime import timedelta

import pytest

from .conftest import DRIVERS
from .harness import World
from .scenarios import effort

SOAK = bool(os.environ.get("BYOBOT_SOAK"))
N = 5000 if SOAK else 1000
BASE = 50
GROWTH = 1.3
SLACK = 0.003  # seconds, so timer noise on fast operations can't fail a check
TRIES = 5
ROUNDS = 3  # interleaved, so a runner slowing down mid-test hits every size alike
REPLAY = "every late event or undo replays the whole log"
UNDONE: dict[int, set] = {}  # each round undoes fresh creations
BUDGETS = {"late_event": 0.05, "undo": 0.05, "offline_sync": 1.0}


@pytest.fixture(scope="module", params=list(DRIVERS))
def driver(request):
    return request.param


def history(driver: str, directory, n: int) -> World:
    world = World(DRIVERS[driver](), directory)
    world.replica("hub")
    for name in ["late"] + [f"phone{i}" for i in range(TRIES)]:
        world.replica(name, skew=timedelta(days=-30) if name == "late" else timedelta(hours=-1))
    for i in range(n):
        world.author("hub", effort(f"e{i}", at=world.clock("hub").read(world.now)))
        world.tick(60)
    return world


@pytest.fixture(scope="module")
def worlds(driver, tmp_path_factory):
    built = {n: history(driver, tmp_path_factory.mktemp(f"n{n}"), n) for n in (BASE, N, 2 * N)}
    yield built
    for world in built.values():
        world.close()


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def late_event(world: World) -> float:
    changes = [world.author("late", effort(f"late{len(world.changes)}")) for _ in range(TRIES)]
    return min(timed(lambda c=c: world.deliver("hub", [c])) for c in changes)


def undo(world: World) -> float:
    done = UNDONE.setdefault(id(world), set())
    creations = [c.id for c in list(world.changes.values())[::7] if c.id not in done][:TRIES]
    done.update(creations)
    return min(timed(lambda c=c: world.author("hub", {"intent": "undo", "change": c})) for c in creations)


def offline_sync(world: World) -> float:
    costs = []
    for i in range(TRIES):
        phone = f"phone{i}"
        workout = [world.author(phone, effort(f"{phone}.{len(world.changes)}")) for _ in range(30)]
        costs.append(timed(lambda w=workout: world.deliver("hub", w)))
    return min(costs)


OPS = {"late_event": late_event, "undo": undo, "offline_sync": offline_sync}


@pytest.mark.baseline(v0=REPLAY)
@pytest.mark.parametrize("op", list(OPS))
def test_s15_cost_does_not_grow_with_history(worlds, op):
    sizes = (BASE, N, 2 * N)
    rounds = [[OPS[op](worlds[n]) for n in sizes] for _ in range(ROUNDS)]
    base, small, large = (min(r[i] for r in rounds) for i in range(len(sizes)))
    ms = ", ".join(f"{c * 1000:.1f} ms at {n}" for c, n in ((base, BASE), (small, N), (large, 2 * N)))
    assert large - base < GROWTH * (small - base) + SLACK, f"{op}: {ms}"


@pytest.mark.skipif(not SOAK, reason="absolute budgets run in the soak tier")
@pytest.mark.baseline(v0=REPLAY)
@pytest.mark.parametrize("op", list(OPS))
def test_s15_operations_stay_within_budget(worlds, op):
    cost = OPS[op](worlds[2 * N])
    assert cost < BUDGETS[op], f"{op}: {cost * 1000:.1f} ms"
