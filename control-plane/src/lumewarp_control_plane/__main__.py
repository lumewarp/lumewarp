"""CLI: `python -m lumewarp_control_plane {serve,migrate,openapi,bootstrap}`."""

import argparse
import asyncio
import json
import sys

from lumewarp_control_plane.settings import Settings


def _serve(settings: Settings, _: argparse.Namespace) -> None:
    import uvicorn

    from lumewarp_control_plane.app import create_app

    host, port = settings.http_host_port
    # One worker: the gRPC server and background loops must run exactly once.
    uvicorn.run(create_app(settings), host=host, port=port, workers=1)


def _migrate(settings: Settings, args: argparse.Namespace) -> None:
    from lumewarp_control_plane import migrate
    from lumewarp_control_plane.db import create_engine

    async def run() -> None:
        engine = create_engine(settings)
        try:
            if args.downgrade:
                await migrate.downgrade(engine, args.downgrade)
            else:
                await migrate.upgrade(engine, args.revision)
            async with engine.connect() as conn:
                print(f"database at revision {await migrate.current_revision(conn)}")
        finally:
            await engine.dispose()

    asyncio.run(run())


def _openapi(settings: Settings, args: argparse.Namespace) -> None:
    from lumewarp_control_plane.app import create_app

    # Building the app does not connect to the database.
    spec = json.dumps(create_app(settings).openapi(), indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as f:
            f.write(spec)
    else:
        sys.stdout.write(spec)


def _bootstrap(settings: Settings, args: argparse.Namespace) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from lumewarp_control_plane import audit
    from lumewarp_control_plane.db import create_engine, create_session_factory
    from lumewarp_control_plane.models import ActorType, GatewayToken, Scope
    from lumewarp_control_plane.security import SecretKind, generate_secret

    scope = args.scope or settings.scope

    async def run() -> str:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session, session.begin():
                await session.execute(insert(Scope).values(name=scope).on_conflict_do_nothing())
                new = generate_secret(SecretKind.GATEWAY_TOKEN)
                token = GatewayToken(
                    scope=scope,
                    gateway_id=args.gateway_id,
                    name=args.name or "",
                    token_hash=new.hash,
                    display_prefix=new.display_prefix,
                )
                session.add(token)
                await session.flush()
                audit.record(
                    session,
                    action="gateway_token.create",
                    resource_type="gateway_token",
                    resource_id=token.id,
                    actor_type=ActorType.SYSTEM,
                    actor_id="bootstrap",
                    details={"gateway_id": args.gateway_id, "scope": scope},
                )
            return new.secret
        finally:
            await engine.dispose()

    secret = asyncio.run(run())
    print(f"scope {scope!r} ready; gateway token for {args.gateway_id!r}:", file=sys.stderr)
    print(secret)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumewarp-control-plane")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the HTTP API (+ gRPC and background loops)")

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("revision", nargs="?", default="head", help="upgrade target (default head)")
    p.add_argument("--downgrade", metavar="REV", help="downgrade to REV instead")

    p = sub.add_parser("openapi", help="print the OpenAPI document (no database needed)")
    p.add_argument("-o", "--output", help="write to a file instead of stdout")

    p = sub.add_parser(
        "bootstrap", help="create the config scope and print a new gateway token (once)"
    )
    p.add_argument("--gateway-id", required=True)
    p.add_argument("--name", help="label for the token")
    p.add_argument("--scope", help="default: LUMEWARP_SCOPE")
    return parser


COMMANDS = {"serve": _serve, "migrate": _migrate, "openapi": _openapi, "bootstrap": _bootstrap}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.command or "serve"](Settings(), args)


if __name__ == "__main__":
    main()
