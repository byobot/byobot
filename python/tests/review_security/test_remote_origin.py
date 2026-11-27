"""Review: a page on another site could reach /mcp, since Origin wasn't checked, which the MCP spec requires against
DNS rebinding."""

from test_remote import BASE, connect, request, running  # noqa: F401


def test_mcp_refuses_a_request_from_another_sites_page(running):  # noqa: F811
    _, tokens = connect(running)
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    resp, _ = request(running, "POST", "/mcp", ping, {**headers, "Origin": "https://attacker.example"})
    assert resp.status == 403
    resp, _ = request(running, "POST", "/mcp", ping, {**headers, "Origin": BASE})
    assert resp.status == 200
