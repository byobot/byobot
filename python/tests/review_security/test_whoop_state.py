"""Review: WHOOP's sign-in state is 32 bits (four random bytes), where 128 is the usual floor."""

import pytest

from byobot import whoop


def test_the_whoop_sign_in_state_has_at_least_128_bits():
    seen = []

    def wait(state):
        seen.append(state)
        raise RuntimeError("stop before the token request")

    with pytest.raises(RuntimeError):
        whoop.connect("example-client", "example-secret", open_browser=lambda url: None, wait=wait)
    assert len(seen[0]) >= 22
