use clap::Parser;
use mock_mcp::{serve, App, Options};
use tokio::net::TcpListener;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let options = Options::parse();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(options.workers)
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        let listener = TcpListener::bind(options.listen).await?;
        eprintln!("mock-mcp listening on {}", listener.local_addr()?);
        let app = App::new(options);
        tokio::select! {
            result = serve(listener, app) => result?,
            result = tokio::signal::ctrl_c() => result?,
        }
        Ok(())
    })
}
