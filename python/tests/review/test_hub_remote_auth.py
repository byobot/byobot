"""Review: `byobot remote --revoke` and `--api-key` run in another process from the server, which keeps codes and
registered clients in memory and rewrites oauth.json from what it read earlier."""

import pytest

from byobot.remote import Auth, OAuthError

from test_remote import BASE, CALLBACK, pkce


@pytest.fixture
def server_auth(byobot_home):
    return Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp")


def test_revoke_also_voids_a_code_the_running_server_already_handed_out(server_auth):
    client = server_auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                                   "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = server_auth.grant(client["client_id"], CALLBACK, challenge)  # approved; the code is on its way back

    assert Auth(server_auth.path).revoke_all() == 0  # `byobot remote --revoke`, from another process

    with pytest.raises(OAuthError):
        server_auth.exchange(server_auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)
    assert server_auth.clients() == [], "a client approved before --revoke is connected after it"


def test_revoke_during_a_refresh_is_not_undone(server_auth):
    client = server_auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                                   "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = server_auth.grant(client["client_id"], CALLBACK, challenge)
    tokens = server_auth.exchange(server_auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)

    # The refresh request has authenticated the client and is about to rotate its tokens when --revoke runs.
    authed = server_auth.authenticate(client["client_id"], None)
    original = server_auth._issue

    def issue_after_a_revoke(c):
        Auth(server_auth.path).revoke_all()
        return original(c)

    server_auth._issue = issue_after_a_revoke
    try:
        fresh = server_auth.refresh(authed, tokens["refresh_token"])
    except OAuthError:
        fresh = None
    server_auth._issue = original
    assert server_auth.clients() == [], "the revoked client is connected again"
    assert fresh is None or server_auth.client_for_token(fresh["access_token"]) is None


def test_an_api_key_made_while_the_server_issues_tokens_is_kept(server_auth):
    client = server_auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                                   "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = server_auth.grant(client["client_id"], CALLBACK, challenge)

    authed = server_auth.authenticate(client["client_id"], None)
    cli = Auth(server_auth.path)  # `byobot remote --api-key example_key`, from another process
    original = server_auth._load
    made = []

    def load_then_the_cli_writes():
        data = original()
        if not made:
            made.append(cli.create_key("example_key"))
        return data

    server_auth._load = load_then_the_cli_writes
    tokens = server_auth.exchange(authed, code, CALLBACK, verifier)
    server_auth._load = original

    assert server_auth.client_for_token(tokens["access_token"]) is not None
    assert server_auth.client_for_token(made[0]) is not None, "the key printed to the user doesn't work"
