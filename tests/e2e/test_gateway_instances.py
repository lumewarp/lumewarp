"""One gateway id as two processes: one token, two instances, neither evicts the other.

docs/config-protocol.md, "Gateway instances": every process is its own instance with its own
config stream and status, the control plane derives the gateway's state from them, and a
published change reaches all of them.
"""

from stack import wait_for


def _instances(stack) -> list[dict]:
    status = stack.gateway_status()
    return status["instances"] if status else []


def _connected(stack) -> list[dict]:
    return [instance for instance in _instances(stack) if instance["connected"]]


def test_two_processes_of_one_gateway_id_both_stay_connected_and_serve(stack):
    seeded = stack.seed()
    first = stack.start_gateway(
        state_dir=stack.dir / "state-one", tag="gateway-one", extra_env={"HOSTNAME": "gw-one"}
    )
    second = stack.start_gateway(
        state_dir=stack.dir / "state-two", tag="gateway-two", extra_env={"HOSTNAME": "gw-two"}
    )

    # The second connection replaced nothing: one gateway id with two connected instances,
    # told apart by the instance id the gateway derives from its hostname.
    wait_for(lambda: len(_connected(stack)) == 2, 15, "two connected gateway instances")
    status = stack.gateway_status()
    assert status["connected"] is True
    instance_ids = {instance["instance_id"] for instance in _connected(stack)}
    assert len(instance_ids) == 2
    assert any(name.startswith("gw-one-") for name in instance_ids), instance_ids
    assert any(name.startswith("gw-two-") for name in instance_ids), instance_ids

    # Both ACK the current version and both serve it.
    version = stack.current_version()
    wait_for(
        lambda: all(instance["acked_version"] == version for instance in _connected(stack)),
        15,
        "both instances ACKed the current version",
    )
    for gateway in (first, second):
        assert gateway.chat(seeded.a.secret).status_code == 200
        assert gateway.chat(seeded.b.secret).status_code == 200

    # A published change reaches both instances, and both stop serving the revoked key.
    stack.ok("POST", f"/keys/{seeded.b.id}/revoke")
    wait_for(
        lambda: all(
            gateway.chat(seeded.b.secret).status_code == 401 for gateway in (first, second)
        ),
        10,
        "both instances reject the revoked key",
    )
    for gateway in (first, second):
        assert gateway.chat(seeded.a.secret).status_code == 200
    version = stack.current_version()
    wait_for(
        lambda: all(instance["acked_version"] == version for instance in _connected(stack)),
        10,
        "both instances ACKed the new version",
    )

    # Losing one instance leaves the gateway connected through the other.
    first.stop()
    wait_for(lambda: len(_connected(stack)) == 1, 15, "one connected instance left")
    status = stack.gateway_status()
    assert status["connected"] is True
    assert [instance["connected"] for instance in _instances(stack)].count(False) == 1
    assert second.chat(seeded.a.secret).status_code == 200
