"""Review: the approval page led with the name a client picked for itself, so a client calling itself after a known
assistant read as that assistant."""

import json
from urllib.parse import urlencode

from test_remote import CALLBACK, pkce, request, running  # noqa: F401


def test_the_approval_page_leads_with_where_the_app_goes(running):  # noqa: F811
    _, raw = request(running, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                          "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "xyz"}
    _, page = request(running, "GET", "/oauth/authorize?" + urlencode(params))
    assert b"calling itself" in page and b"<b>client.example</b>" in page
