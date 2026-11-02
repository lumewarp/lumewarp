# Build from the repository root so the workspace path dependencies and
# lumewarp-proto's `../../../proto` build input are both in the build context:
#
#   docker build -f gateway/Dockerfile -t lumewarp-gateway:0.1.0 .
#
# gateway/Cargo.toml declares rust-version = "1.88", but Cargo.lock pins
# uuid 1.28.0, which requires rustc 1.89. Use the lowest toolchain that builds
# the checked-in lockfile; bump the workspace MSRV (or pin uuid) to restore 1.88.
# gateway/rust-toolchain.toml requests the floating "stable" channel; it is not
# copied here so rustup cannot pull a newer toolchain during the build.
FROM rust:1.89-slim-bookworm AS builder

ENV RUSTUP_TOOLCHAIN=1.89.0

WORKDIR /src

COPY gateway/Cargo.toml gateway/Cargo.lock ./gateway/
COPY gateway/crates ./gateway/crates
COPY gateway/strategies ./gateway/strategies
COPY proto ./proto

WORKDIR /src/gateway
RUN cargo build --release --locked -p lumewarp-gateway

# The runtime is distroless (no shell), so create the snapshot directory here and
# copy it with the nonroot owner. A plain `docker run` can then write to the
# default state directory; Kubernetes mounts an emptyDir or PVC over it.
RUN mkdir -p /out/state

FROM gcr.io/distroless/cc-debian12:nonroot AS runtime

COPY --from=builder --chown=65532:65532 /src/gateway/target/release/lumewarp /usr/local/bin/lumewarp
COPY --from=builder --chown=65532:65532 /out/state /var/lib/lumewarp
COPY LICENSE /licenses/LICENSE

USER 65532:65532
EXPOSE 4000 9464
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/local/bin/lumewarp"]
