"""Deep review (connectors): WHOOP imports against WHOOP's rate limit, busy answers and token refreshes.

`collect` gathers every page of every type before anything is written, and `Client.request` gives up on the first
429 or 5xx. WHOOP allows 100 requests a minute, so an import of more than about two years of history (four
endpoints, 25 records a page) can never finish: each try is cut short at the 101st request and writes nothing.
"""

import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _conn_helpers as h  # noqa: E402

from byobot import credentials, whoop  # noqa: E402

UNTIL = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _fake_clock(monkeypatch, fake: h.FakeWhoop) -> None:
    """Waiting moves the fake clock on instead of the test's."""
    monkeypatch.setattr(time, "sleep", fake.sleep)
    monkeypatch.setattr(time, "time", fake.time)


def _held(store) -> int:
    return store.rows("SELECT count(*) AS n FROM source_records WHERE source = 'whoop'")[0]["n"]


def test_conn_a_long_history_import_finishes_under_whoops_rate_limit(monkeypatch, store):
    h.connect()
    records = h.history(900, UNTIL)  # about 120 requests: more than WHOOP answers in one minute
    fake = h.FakeWhoop(records, per_minute=100)
    _fake_clock(monkeypatch, fake)
    out = h.import_whoop(monkeypatch, fake, days=1000)
    total = sum(len(v) for v in records.values())
    assert out["fetched"] == total
    assert _held(store) == total


@pytest.mark.parametrize("status", [429, 502, 503])
def test_conn_one_busy_answer_mid_import_is_tried_again(monkeypatch, store, status):
    h.connect()
    records = h.history(60, UNTIL)
    fake = h.FakeWhoop(records, fail={3: status})  # the third page answers busy once
    _fake_clock(monkeypatch, fake)
    out = h.import_whoop(monkeypatch, fake, days=90)
    total = sum(len(v) for v in records.values())
    assert out["fetched"] == total and _held(store) == total


class _NoRotation(h.FakeWhoop):
    """A token endpoint that renews the access token without sending a new refresh token, as RFC 6749 section
    6 allows; the refresh token sent stays good."""

    def __call__(self, req):
        if req.get_method() == "POST":
            self.refreshed = True
            return io.BytesIO(json.dumps({"access_token": "access-2", "expires_in": 3600,
                                          "token_type": "bearer"}).encode())
        return super().__call__(req)


def test_conn_a_refresh_without_a_new_refresh_token_keeps_the_old_one(monkeypatch, store):
    h.connect(expires_in=-10)  # the access token has run out
    records = h.history(3, UNTIL)
    fake = _NoRotation(records)
    try:
        out = h.import_whoop(monkeypatch, fake, days=30)
    except KeyError as exc:  # on main: a bare KeyError from whoop._tokens, not an answer
        pytest.fail(f"import crashed with KeyError {exc}")
    assert out["fetched"] == sum(len(v) for v in records.values())
    kept = credentials.load(whoop.NAME)
    assert kept["access_token"] == "access-2" and kept["refresh_token"] == "refresh-1"
