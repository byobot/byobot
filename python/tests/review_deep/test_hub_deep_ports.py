"""Hub deep review: `serve` and `remote` pick ports from overlapping ranges.

`serve.choose_port` gives a named profile the first free port from 8767 up to 8864; `remote.choose_port` uses 8780
for the default profile and 8781 up for named ones. Whichever starts first while the other isn't running takes
the port and saves it (`serve/port`, `remote/port`), so the other can no longer start on its saved or default
port, and a paired phone or a connected assistant loses its laptop until someone passes `--port`.
"""

import socket

from byobot import remote, serve


class _Busy(socket.socket):
    """Ports below 8780 are taken by other programs on this computer."""

    def bind(self, address):
        if address[1] < remote.DEFAULT_PORT:
            raise OSError(98, "Address already in use")
        return super().bind((address[0], 0))


def test_a_named_profile_hub_never_takes_the_remote_port(monkeypatch):
    monkeypatch.setenv("BYOBOT_PROFILE", "example")
    monkeypatch.setattr(serve.socket, "socket", _Busy)
    port = serve.choose_port()
    remote_ports = range(remote.DEFAULT_PORT, remote.DEFAULT_PORT + 100)
    assert port not in remote_ports, f"serve chose {port}, which `byobot remote` uses or will choose"
