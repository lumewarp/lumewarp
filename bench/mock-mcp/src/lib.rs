//! A controllable MCP upstream for Lumewarp tests and benchmarks.
//!
//! One process serves any number of named instances at `/s/<name>/mcp`. The admin
//! API at `/admin/*` creates instances, replaces their tool catalog while running,
//! injects faults, and reads or clears the bounded request record.

mod admin;
mod instance;
mod model;
mod protocol;
mod records;
mod state;

pub use model::{
    Auth, Behaviour, CacheScope, Era, ErrorSpec, Fault, FaultKind, InstanceConfig, RequestRecord,
    SessionMode, Shape, ToolConfig,
};
pub use records::RecordFilter;

use arc_swap::ArcSwap;
use bytes::Bytes;
use clap::Parser;
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use std::collections::HashMap;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;

pub type BoxError = Box<dyn std::error::Error + Send + Sync>;
pub type Body = http_body_util::combinators::UnsyncBoxBody<Bytes, BoxError>;

#[derive(Clone, Debug, Parser)]
pub struct Options {
    #[arg(long, default_value = "127.0.0.1:9201")]
    pub listen: SocketAddr,
    #[arg(long, default_value_t = default_workers(), value_parser = parse_workers)]
    pub workers: usize,
    /// Disable recording entirely (the benchmark path).
    #[arg(long)]
    pub no_record: bool,
    #[arg(long, default_value_t = 2048)]
    pub record_capacity: usize,
    #[arg(long, default_value_t = 65_536)]
    pub record_body_limit: usize,
    #[arg(long, default_value_t = 16 * 1024 * 1024)]
    pub max_body_bytes: usize,
}

fn parse_workers(value: &str) -> Result<usize, String> {
    value
        .parse::<usize>()
        .ok()
        .filter(|&n| n > 0)
        .ok_or_else(|| "workers must be a positive integer".into())
}

fn default_workers() -> usize {
    std::thread::available_parallelism().map_or(1, usize::from)
}

pub struct App {
    pub(crate) options: Options,
    instances: ArcSwap<HashMap<String, Arc<instance::Instance>>>,
    pub(crate) records: records::RecordLog,
}

impl App {
    pub fn new(options: Options) -> Arc<Self> {
        Arc::new(Self {
            records: records::RecordLog::new(
                options.record_capacity,
                options.record_body_limit,
                !options.no_record,
            ),
            instances: ArcSwap::from_pointee(HashMap::new()),
            options,
        })
    }

    pub fn instance(&self, name: &str) -> Option<Arc<instance::Instance>> {
        self.instances.load().get(name).cloned()
    }

    pub fn instance_configs(&self) -> Vec<InstanceConfig> {
        let mut configs: Vec<_> = self
            .instances
            .load()
            .values()
            .map(|instance| instance.config())
            .collect();
        configs.sort_by(|a, b| a.name.cmp(&b.name));
        configs
    }

    pub fn create_instance(
        &self,
        config: InstanceConfig,
    ) -> Result<Arc<instance::Instance>, String> {
        if self.instance(&config.name).is_some() {
            return Err(format!("server '{}' already exists", config.name));
        }
        let instance = instance::Instance::new(config)?;
        let mut map = (*self.instances.load_full()).clone();
        map.insert(instance.name.clone(), instance.clone());
        self.instances.store(Arc::new(map));
        Ok(instance)
    }

    pub fn replace_instance(
        &self,
        name: &str,
        config: InstanceConfig,
    ) -> Result<Arc<instance::Instance>, String> {
        if config.name != name {
            return Err("the body name must match the path".into());
        }
        let instance = instance::Instance::new(config)?;
        let mut map = (*self.instances.load_full()).clone();
        if !map.contains_key(name) {
            return Err(format!("server '{name}' does not exist"));
        }
        map.insert(name.to_string(), instance.clone());
        self.instances.store(Arc::new(map));
        Ok(instance)
    }

    pub fn delete_instance(&self, name: &str) -> Result<bool, String> {
        let mut map = (*self.instances.load_full()).clone();
        let existed = map.remove(name).is_some();
        self.instances.store(Arc::new(map));
        Ok(existed)
    }

    pub fn upsert_tool(&self, name: &str, tool: ToolConfig) -> Result<InstanceConfig, String> {
        let Some(instance) = self.instance(name) else {
            return Err(format!("server '{name}' does not exist"));
        };
        let mut config = instance.config();
        match config
            .tools
            .iter_mut()
            .find(|existing| existing.name == tool.name)
        {
            Some(existing) => *existing = tool,
            None => config.tools.push(tool),
        }
        instance.replace_tools(config.tools)?;
        Ok(instance.config())
    }

    pub fn remove_tool(&self, name: &str, tool: &str) -> Result<InstanceConfig, String> {
        let Some(instance) = self.instance(name) else {
            return Err(format!("server '{name}' does not exist"));
        };
        let mut config = instance.config();
        let before = config.tools.len();
        config.tools.retain(|existing| existing.name != tool);
        if config.tools.len() == before {
            return Err(format!("tool '{tool}' does not exist"));
        }
        instance.replace_tools(config.tools)?;
        Ok(instance.config())
    }

    pub fn set_tools(&self, name: &str, tools: Vec<ToolConfig>) -> Result<InstanceConfig, String> {
        let Some(instance) = self.instance(name) else {
            return Err(format!("server '{name}' does not exist"));
        };
        instance.replace_tools(tools)?;
        Ok(instance.config())
    }

    pub fn set_fault(&self, name: &str, fault: Option<Fault>) -> Result<InstanceConfig, String> {
        let Some(instance) = self.instance(name) else {
            return Err(format!("server '{name}' does not exist"));
        };
        instance.set_fault(fault)?;
        Ok(instance.config())
    }

    pub fn record_snapshot(&self, filter: &RecordFilter) -> Vec<RequestRecord> {
        self.records.snapshot(filter)
    }

    pub fn clear_records(&self, server: Option<&str>) -> usize {
        self.records.clear(server)
    }

    pub fn recording_enabled(&self) -> bool {
        self.records.enabled()
    }

    pub fn set_recording(&self, enabled: bool) {
        self.records.set_enabled(enabled);
    }

    async fn respond(&self, request: Request<Incoming>) -> Result<Response<Body>, Infallible> {
        let path = request.uri().path().to_string();
        if path == "/health" {
            if request.method() != hyper::Method::GET {
                return Ok(protocol::admin_empty(
                    StatusCode::METHOD_NOT_ALLOWED.as_u16(),
                ));
            }
            let body = serde_json::json!({
                "status": "ok",
                "servers": self.instances.load().len(),
                "records": self.records.len(),
                "recording": self.records.enabled(),
            });
            return Ok(protocol::admin_json(200, &body));
        }
        if path == "/admin" || path.starts_with("/admin/") {
            return Ok(admin::handle(self, request).await);
        }
        if let Some(name) = mcp_instance_name(&path) {
            return Ok(self.handle_mcp(name, request).await);
        }
        Ok(protocol::admin_json(
            404,
            &serde_json::json!({"error": "not found"}),
        ))
    }

    async fn handle_mcp(&self, name: String, request: Request<Incoming>) -> Response<Body> {
        let (parts, body) = request.into_parts();
        let record = self
            .records
            .begin(&name, parts.method.as_str(), parts.uri.path());
        if let Some(record) = &record {
            records::annotate(&Some(record.clone()), |record| {
                for (header, value) in parts.headers.iter() {
                    record
                        .headers
                        .entry(header.as_str().to_string())
                        .or_default()
                        .push(String::from_utf8_lossy(value.as_bytes()).into_owned());
                }
                record.session_id = parts
                    .headers
                    .get("mcp-session-id")
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_string);
            });
        }

        let body = match Limited::new(body, self.options.max_body_bytes)
            .collect()
            .await
        {
            Ok(collected) => collected.to_bytes(),
            Err(_) => {
                records::finish(&record, "error", 413, "text/plain", None, None);
                return protocol::admin_empty(413);
            }
        };
        let parsed = serde_json::from_slice::<serde_json::Value>(&body).ok();
        if let Some(record) = &record {
            let (text, truncated) = self.records.body(&body);
            let (method, id, tool, _version) = parsed
                .as_ref()
                .map(protocol::quick_metadata)
                .unwrap_or((None, None, None, None));
            records::annotate(&Some(record.clone()), |record| {
                record.body = text;
                record.body_truncated = truncated;
                record.method = method;
                record.request_id = id;
                record.tool = tool;
            });
        }

        let Some(instance) = self.instance(&name) else {
            records::finish(
                &record,
                "unknown_server",
                404,
                "application/json",
                None,
                None,
            );
            return protocol::admin_json(
                404,
                &serde_json::json!({"error": format!("unknown server: {name}")}),
            );
        };
        protocol::handle(self, instance, parts, body, parsed, record).await
    }
}

fn mcp_instance_name(path: &str) -> Option<String> {
    // `/s/<name>/mcp`
    let rest = path.strip_prefix("/s/")?;
    let (name, tail) = rest.split_once('/')?;
    (tail == "mcp" && !name.is_empty()).then(|| name.to_string())
}

pub async fn serve(listener: TcpListener, app: Arc<App>) -> std::io::Result<()> {
    loop {
        let (socket, _) = match listener.accept().await {
            Ok(connection) => connection,
            Err(error) => {
                eprintln!("mock-mcp accept error: {error}");
                tokio::time::sleep(std::time::Duration::from_millis(1)).await;
                continue;
            }
        };
        let _ = socket.set_nodelay(true);
        let app = app.clone();
        tokio::spawn(async move {
            let service = service_fn(move |request| {
                let app = app.clone();
                async move { app.respond(request).await }
            });
            let _ = http1::Builder::new()
                .keep_alive(true)
                .serve_connection(TokioIo::new(socket), service)
                .await;
        });
    }
}

/// A tiny convenience used by tests and by the fixture's health check.
pub fn full_body(bytes: Bytes) -> Body {
    Full::new(bytes)
        .map_err(|never| match never {})
        .boxed_unsync()
}
