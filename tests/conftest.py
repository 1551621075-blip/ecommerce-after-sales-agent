import socket

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Unit tests must use in-memory transports, not network sockets")

    async def denied_async(*args, **kwargs):
        denied()

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)
