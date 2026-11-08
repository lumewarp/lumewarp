//! CLI for the example MCP server and A2A agent.
//!
//! ```sh
//! mcp-a2a-mock mcp --listen 127.0.0.1:9110
//! mcp-a2a-mock a2a --listen 127.0.0.1:9120
//! ```

use clap::{Parser, Subcommand};
use mock_upstream::{
    a2a::{serve_a2a, A2aOptions, A2aServer},
    mcp::{serve_mcp, McpOptions, McpServer},
};
use std::{error::Error, sync::Arc};
use tokio::net::TcpListener;

#[derive(Parser)]
#[command(
    name = "mcp-a2a-mock",
    version,
    about = "Example MCP server and A2A agent for Lumewarp development and tests"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Serve the example MCP server (Streamable HTTP).
    Mcp(McpOptions),
    /// Serve the example A2A agent (v1.0 JSON-RPC + SSE).
    A2a(A2aOptions),
}

fn main() -> Result<(), Box<dyn Error>> {
    let cli = Cli::parse();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        match cli.command {
            Command::Mcp(options) => {
                let listener = TcpListener::bind(options.listen).await?;
                let path = options.path.clone();
                eprintln!(
                    "mcp server listening on http://{}{path} (control: /_mock/)",
                    listener.local_addr()?
                );
                let server = Arc::new(McpServer::new(options)?);
                tokio::select! {
                    result = serve_mcp(listener, server) => result?,
                    result = tokio::signal::ctrl_c() => result?,
                }
            }
            Command::A2a(options) => {
                let listener = TcpListener::bind(options.listen).await?;
                let path = options.path.clone();
                eprintln!(
                    "a2a agent listening on http://{}{path} \
                     (card: /.well-known/agent-card.json, control: /_mock/)",
                    listener.local_addr()?
                );
                let server = Arc::new(A2aServer::new(options)?);
                tokio::select! {
                    result = serve_a2a(listener, server) => result?,
                    result = tokio::signal::ctrl_c() => result?,
                }
            }
        }
        Ok(())
    })
}
