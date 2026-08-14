"""Shared fixtures for sysops_mcp tests."""

import socket
import threading

import pytest


@pytest.fixture
def unused_tcp_listener():
    """Open a real TCP listener on an ephemeral port for check_port tests."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept_once():
        try:
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    yield "127.0.0.1", port
    server.close()
