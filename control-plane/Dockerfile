# Build from the repository root so both the UI and the control plane are in the
# build context:
#
#   docker build -f control-plane/Dockerfile -t lumewarp-control-plane:0.1.0 .
#
# The first stage builds the Admin UI; its dist output is copied into the
# control-plane package's static directory, exactly like `pnpm build:cp` does
# for local development.
FROM node:24-bookworm-slim AS ui

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

WORKDIR /src/ui

COPY ui/package.json ui/pnpm-lock.yaml ui/.pnpmfile.cjs ./
RUN corepack enable && corepack prepare pnpm@11.2.2 --activate
RUN pnpm install --frozen-lockfile

COPY ui/ ./
RUN pnpm build

# Install the locked runtime dependencies and the package (non-editable) into a
# venv at its final path, so console-script shebangs remain valid in the runtime
# stage. Dev dependencies are excluded.
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app/control-plane

# Cache dependency resolution/installation before copying the source.
COPY control-plane/pyproject.toml control-plane/uv.lock control-plane/README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY control-plane/src ./src
RUN rm -rf src/lumewarp_control_plane/static \
    && mkdir -p src/lumewarp_control_plane/static
COPY --from=ui /src/ui/dist/ ./src/lumewarp_control_plane/static/

# Installs the package non-editable; hatchling includes the static directory as
# package data (see [tool.hatch.build.targets.wheel] artifacts in pyproject.toml).
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

RUN groupadd --gid 10001 lumewarp \
    && useradd --uid 10001 --gid lumewarp --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin lumewarp

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY LICENSE /licenses/LICENSE

USER 10001:10001
EXPOSE 8091 50051
STOPSIGNAL SIGTERM

ENTRYPOINT ["/app/.venv/bin/python", "-m", "lumewarp_control_plane"]
CMD ["serve"]
