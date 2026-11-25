from byobot.core import hlc


def test_readings_compare_as_text_and_round_trip():
    a, b = hlc.format((1_795_100_000_000, 9)), hlc.format((1_795_100_000_000, 10))
    assert a < b and hlc.parse(b) == (1_795_100_000_000, 10)


def test_tick_follows_the_wall_clock_and_never_goes_back():
    assert hlc.tick((100, 4), 200) == (200, 0)
    assert hlc.tick((100, 4), 100) == (100, 5)
    assert hlc.tick((100, 4), 50) == (100, 5)
    assert hlc.tick((100, 999_999), 50) == (101, 0)


def test_a_day_ahead_is_a_runaway_clock():
    assert hlc.runaway((hlc.RUNAWAY_MS + 2, 0), 1)
    assert not hlc.runaway((3_600_000, 0), 0)
