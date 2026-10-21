"""Tiny gateway stand-in: connect to ConfigService, receive the first update, ACK it.

    uv run python -m tests.integration.grpc_probe --target 127.0.0.1:50051 \\
        --gateway-id gw-dev --token "$(cat token)"

Prints the version and key count of the snapshot it received and exits 0, so the control
plane can be checked end to end without grpcurl. Also used by the integration tests.
"""

import argparse
import asyncio
import sys

import grpc

from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    config_service_pb2 as cpb,
)
from lumewarp_control_plane._proto.lumewarp.config.v1 import (
    config_service_pb2_grpc as cpb_grpc,
)


async def probe(target: str, token: str, gateway_id: str, timeout: float = 10.0) -> tuple[int, int]:
    """Return (version, key count) of the full snapshot the control plane sends first."""
    async with grpc.aio.insecure_channel(target) as channel:
        call = cpb_grpc.ConfigServiceStub(channel).Stream(
            metadata=(("authorization", f"Bearer {token}"),)
        )
        await call.write(
            cpb.GatewayMessage(hello=cpb.Hello(gateway_id=gateway_id, gateway_version="probe"))
        )
        update = await asyncio.wait_for(call.read(), timeout)
        if update.WhichOneof("update") != "full":
            raise RuntimeError(f"expected a full snapshot, got {update.WhichOneof('update')}")
        await call.write(cpb.GatewayMessage(ack=cpb.Ack(version=update.full.version)))
        await call.done_writing()
        call.cancel()
        return update.full.version, len(update.full.api_keys)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="127.0.0.1:50051")
    parser.add_argument("--gateway-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    try:
        version, keys = asyncio.run(probe(args.target, args.token, args.gateway_id))
    except Exception as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"received full snapshot version {version} with {keys} keys; ACKed")


if __name__ == "__main__":
    main()
