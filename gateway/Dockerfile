# Build from the repository root so the workspace path dependencies and
# lumewarp-proto's `../../../proto` build input are both in the build context:
#
#   docker build -f gateway/Dockerfile -t lumewarp-gateway:0.1.0 .
#
# gateway/Cargo.toml declares rust-version = "1.89", and Cargo.lock pins uuid 1.28.0,
# which needs rustc 1.89 too. The toolchain is pinned explicitly here:
# gateway/rust-toolchain.toml requests the floating "stable" channel, and it is not
# copied into the image, so rustup cannot pull another toolchain during the build.
FROM rust:1.89-slim-bookworm AS builder

ENV RUSTUP_TOOLCHAIN=1.89.0

WORKDIR /src

COPY gateway/Cargo.toml gateway/Cargo.lock ./gateway/
COPY gateway/crates ./gateway/crates
# lumewarp-plugin-host's `wasmtime::component::bindgen!` (and the SDK's
# `wit_bindgen::generate!`) resolve `../../wit` at compile time.
COPY gateway/wit ./gateway/wit
COPY proto ./proto
# lumewarp-gateway's dev-dependency `mock-upstream` lives outside the workspace; cargo
# loads its manifest while resolving the workspace even for a non-test build.
COPY bench/mock-upstream/Cargo.toml ./bench/mock-upstream/
COPY bench/mock-upstream/src ./bench/mock-upstream/src
# gateway/plugins is deliberately not copied: a plugin is a file in one of the three
# runtime directories (LUMEWARP_ROUTER_PLUGIN_DIR, LUMEWARP_AUTH_PLUGIN_DIR,
# LUMEWARP_A2A_MIDDLEWARE_DIR), so replacing one is a file and a restart, never an image
# build. The example components are built by gateway/plugins/build.sh and mounted.

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
