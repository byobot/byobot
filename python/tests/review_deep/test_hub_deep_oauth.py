"""Hub deep review, `byobot remote`: refresh token rotation detects replay.

MCP clients register as public clients (`token_endpoint_auth_method: none`), so a refresh token alone is enough to
get new tokens. OAuth 2.1 (section 4.3.1) asks a server that relies on rotation for public clients to treat a
used refresh token presented again as a sign it was stolen, and to revoke the refresh token that replaced it.
`Auth.refresh` (remote.py) only answers `invalid_grant` to the replay: whoever refreshed first (a thief with a
copied token) keeps a working chain for 90 days, and the person's assistant just fails with no sign why.
"""

import base64
import hashlib
import secrets

import pytest

from byobot.remote import Auth, OAuthError

BASE = "https://example-laptop.example.ts.net"
CALLBACK = "https://client.example/callback"


def test_a_replayed_refresh_token_revokes_the_chain(byobot_home):
    auth = Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp")
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                            "token_endpoint_auth_method": "none"})
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    first = auth.exchange(auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)
    c = auth.authenticate(client["client_id"], None)

    stolen = auth.refresh(c, first["refresh_token"])  # a copy of the token is used first, elsewhere
    with pytest.raises(OAuthError):
        auth.refresh(c, first["refresh_token"])  # the assistant's own refresh: a replay
    with pytest.raises(OAuthError):
        auth.refresh(c, stolen["refresh_token"])  # the replay should have revoked what replaced the token
