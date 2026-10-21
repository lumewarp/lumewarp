//! Generates prost messages and tonic client stubs from `proto/` with protox,
//! so no system `protoc` is required.

use std::path::PathBuf;

const FILES: [&str; 3] = [
    "lumewarp/config/v1/snapshot.proto",
    "lumewarp/config/v1/config_service.proto",
    "lumewarp/usage/v1/usage.proto",
];

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?).join("../../../proto");
    for file in FILES {
        println!("cargo:rerun-if-changed={}", root.join(file).display());
    }
    let descriptors = protox::compile(FILES, [&root])?;
    tonic_prost_build::configure()
        .build_server(false)
        .compile_fds(descriptors)?;
    Ok(())
}
