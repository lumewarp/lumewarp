"""Two gateways on one token: each process gets its own id, stream and status row.

A gateway is one process. Both processes generate an id from HOSTNAME at startup, share
the stack's token, and the flat fleet API lists them as two gateways. A published change
reaches both, and losing one leaves the other connected.
"""

from stack import wait_for


def _gateways(stack) -> list[dict]:
    return stack.gateway_items()


def _connected(stack) -> list[dict]:
    return [gateway for gateway in _gateways(stack) if gateway["connected"]]


def test_two_gateways_on_one_token_both_stay_connected_and_serve(stack):
    seeded = stack.seed()
    first = stack.start_gateway(
        state_dir=stack.dir / "state-one", tag="gateway-one", extra_env={"HOSTNAME": "gw-one"}
    )
    second = stack.start_gateway(
        state_dir=stack.dir / "state-two", tag="gateway-two", extra_env={"HOSTNAME": "gw-two"}
    )

    # Neither connection replaced the other: two processes, two gateway ids, one token.
    wait_for(lambda: len(_connected(stack)) == 2, 15, "two connected gateways")
    rows = _connected(stack)
    gateway_ids = {row["gateway_id"] for row in rows}
    assert len(gateway_ids) == 2
    assert any(name.startswith("gw-one-") for name in gateway_ids), gateway_ids
    assert any(name.startswith("gw-two-") for name in gateway_ids), gateway_ids
    assert len({row["token"]["id"] for row in rows}) == 1, rows

    # Both ACK the current version and both serve it.
    version = stack.current_version()
    wait_for(
        lambda: all(gateway["acked_version"] == version for gateway in _connected(stack)),
        15,
        "both gateways ACKed the current version",
    )
    for gateway in (first, second):
        assert gateway.chat(seeded.a.secret).status_code == 200
        assert gateway.chat(seeded.b.secret).status_code == 200

    # A published change reaches both gateways, and both stop serving the revoked key.
    stack.ok("POST", f"/keys/{seeded.b.id}/revoke")
    wait_for(
        lambda: all(
            gateway.chat(seeded.b.secret).status_code == 401 for gateway in (first, second)
        ),
        10,
        "both gateways reject the revoked key",
    )
    for gateway in (first, second):
        assert gateway.chat(seeded.a.secret).status_code == 200
    version = stack.current_version()
    wait_for(
        lambda: all(gateway["acked_version"] == version for gateway in _connected(stack)),
        10,
        "both gateways ACKed the new version",
    )

    # Losing one gateway leaves the other connected; the lost row stays until retention.
    first.stop()
    wait_for(lambda: len(_connected(stack)) == 1, 15, "one connected gateway left")
    remaining = _connected(stack)
    assert remaining[0]["gateway_id"].startswith("gw-two-"), remaining
    stopped = first.status()
    assert stopped is not None and stopped["connected"] is False
    assert stopped["gateway_id"].startswith("gw-one-"), stopped
    assert second.chat(seeded.a.secret).status_code == 200
