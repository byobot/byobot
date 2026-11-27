"""Review L5: an unauthenticated hello names the computer, often after its owner, even with pairing closed."""

import threading

from byobot import serve
from test_serve import Client


def test_hello_names_the_computer_only_while_pairing_is_open(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0, pair=False, log=lambda line: None)
    srv.name = "Example Person's laptop"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = Client(info["port"], info["fingerprint"]).call("GET", "/v1/hello")
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200 and body["pairing_open"] is False
    assert body["server"]["name"] == "byobot"
