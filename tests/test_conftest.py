"""The suite's own guard. A check nothing verifies is a comment."""

import socket

import pytest


def test_opening_a_connection_is_an_error_here():
    # TEST-NET-1, reserved for documentation: if the guard ever came off, this times out
    # against nothing rather than reaching a real host.
    with pytest.raises(AssertionError, match="opened a connection"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def test_resolving_a_name_is_still_allowed():
    """`refuse.check` resolves hostnames to catch a public name pointing at a private address.
    Blocking that would stop testing the thing."""
    assert socket.getaddrinfo("localhost", 80)
