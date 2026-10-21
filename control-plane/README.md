# Lumewarp control plane

Management API, config compiler and plugin host. Never on the gateway request path.

```sh
uv sync
uv run lumewarp-control-plane   # http://127.0.0.1:8080
uv run pytest
```

SQLAlchemy/Alembic, Authlib and grpcio get added when P1 starts.
