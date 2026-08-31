"""Nothing in this suite opens a connection.

Every test here answers from a mock transport or a temporary directory, and until a browser was
installed on one machine that was true by construction rather than by enforcement. It stopped
being true silently: `render` makes its own request and honors no injected client, so three tests
that pass a mock client had been fetching the live page behind it, and the only visible symptom
was the suite taking a hundred seconds instead of seven.

A suite that can reach the network is a suite whose results depend on which machine ran it. This
makes the attempt an error rather than a slowdown, and names the address so the offending path is
obvious rather than inferred.

Name resolution is deliberately still allowed: `refuse.check` resolves hostnames to catch a
public name pointing at a private address, so blocking that would stop testing the thing.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, address):
        raise AssertionError(
            f"a test opened a connection to {address}. Answer from a mock transport instead -- "
            f"a result that depends on a remote host is not a test of this package."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
