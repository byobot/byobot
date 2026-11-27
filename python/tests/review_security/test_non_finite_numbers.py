"""Review: a sample or event holding Infinity or NaN (which Python's JSON reader accepts, and 1e999 becomes) crashes
the write with ValueError instead of being refused, so the hub answers 500."""

import json

import pytest

from byobot.core import relay
from byobot.util import ValidationFailed


def sample(value):
    return {"source": "apple_health", "source_id": "S1", "type": "HKQuantityTypeIdentifierBodyMass",
            "start": "2026-11-23T07:00:00Z", "value": value, "unit": "kg"}


@pytest.mark.parametrize("text", ["Infinity", "NaN", "1e999"])
def test_a_non_finite_number_in_a_sample_is_refused(store, text):
    body = json.loads('{"samples": [%s]}' % json.dumps(sample(0)).replace('"value": 0', f'"value": {text}'))
    with pytest.raises(ValidationFailed):
        relay.accept_samples(store, body)
    assert store.position() == 0
