//! Admin API: create instances, change tools or faults while running, read records.

use crate::model::{
    Fault, InstanceConfig, RequestRecord, SetFault, SetRecording, SetTools, ToolConfig,
};
use crate::records::RecordFilter;
use crate::{protocol, App, Body};
use bytes::Bytes;
use http_body_util::{BodyExt, Limited};
use hyper::body::Incoming;
use hyper::{Method, Request, Response};
use percent_encoding::percent_decode_str;
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use std::collections::HashMap;

pub async fn handle(app: &App, request: Request<Incoming>) -> Response<Body> {
    let method = request.method().clone();
    let path = request.uri().path().to_string();
    let query = request.uri().query().unwrap_or("").to_string();
    let segments: Vec<String> = path
        .strip_prefix("/admin")
        .unwrap_or("")
        .split('/')
        .filter(|segment| !segment.is_empty())
        .map(decode_segment)
        .collect();

    match (method, segments.as_slice()) {
        (Method::GET, [servers]) if servers == "servers" => list_servers(app),
        (Method::POST, [servers]) if servers == "servers" => {
            let config = match body::<InstanceConfig>(request, app).await {
                Ok(config) => config,
                Err(response) => return response,
            };
            if let Err(message) = config.validate() {
                return error(400, &message);
            }
            match app.create_instance(config) {
                Ok(instance) => protocol::admin_json(
                    201,
                    &serde_json::to_value(instance.config()).expect("config"),
                ),
                Err(message) => error(409, &message),
            }
        }
        (Method::GET, [servers, name]) if servers == "servers" => match app.instance(name) {
            Some(instance) => protocol::admin_json(
                200,
                &serde_json::to_value(instance.config()).expect("config"),
            ),
            None => error(404, &format!("server '{name}' does not exist")),
        },
        (Method::PUT, [servers, name]) if servers == "servers" => {
            let mut config = match body::<InstanceConfig>(request, app).await {
                Ok(config) => config,
                Err(response) => return response,
            };
            if config.name.is_empty() {
                config.name = name.clone();
            }
            if config.name != *name {
                return error(400, "the body name must match the path");
            }
            if let Err(message) = config.validate() {
                return error(400, &message);
            }
            match app.replace_instance(name, config) {
                Ok(instance) => protocol::admin_json(
                    200,
                    &serde_json::to_value(instance.config()).expect("config"),
                ),
                Err(message) => error(404, &message),
            }
        }
        (Method::DELETE, [servers, name]) if servers == "servers" => {
            match app.delete_instance(name) {
                Ok(true) => protocol::admin_json(200, &json!({"deleted": true})),
                Ok(false) => error(404, &format!("server '{name}' does not exist")),
                Err(message) => error(500, &message),
            }
        }
        (Method::PUT, [servers, name, tools]) if servers == "servers" && tools == "tools" => {
            let set = match body::<SetTools>(request, app).await {
                Ok(set) => set,
                Err(response) => return response,
            };
            if app.instance(name).is_none() {
                return error(404, &format!("server '{name}' does not exist"));
            }
            match app.set_tools(name, set.tools) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(400, &message),
            }
        }
        (Method::POST, [servers, name, tools]) if servers == "servers" && tools == "tools" => {
            let tool = match body::<ToolConfig>(request, app).await {
                Ok(tool) => tool,
                Err(response) => return response,
            };
            match app.upsert_tool(name, tool) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(400, &message),
            }
        }
        (Method::PUT, [servers, name, tools, tool])
        | (Method::POST, [servers, name, tools, tool])
            if servers == "servers" && tools == "tools" =>
        {
            let mut config = match body::<ToolConfig>(request, app).await {
                Ok(tool) => tool,
                Err(response) => return response,
            };
            if config.name.is_empty() {
                config.name = tool.clone();
            }
            if config.name != *tool {
                return error(400, "the body tool name must match the path");
            }
            match app.upsert_tool(name, config) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(404, &message),
            }
        }
        (Method::DELETE, [servers, name, tools, tool])
            if servers == "servers" && tools == "tools" =>
        {
            match app.remove_tool(name, tool) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(404, &message),
            }
        }
        (Method::POST, [servers, name, fault]) if servers == "servers" && fault == "fault" => {
            let value = match raw_body(request, app).await {
                Ok(value) => value,
                Err(response) => return response,
            };
            let fault = if value.get("fault").is_some() || value.is_null() {
                match serde_json::from_value::<SetFault>(value) {
                    Ok(set) => set.fault,
                    Err(_) => return error(400, "invalid fault object"),
                }
            } else {
                match serde_json::from_value::<Fault>(value) {
                    Ok(fault) => Some(fault),
                    Err(_) => return error(400, "invalid fault object"),
                }
            };
            match app.set_fault(name, fault) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(400, &message),
            }
        }
        (Method::DELETE, [servers, name, fault]) if servers == "servers" && fault == "fault" => {
            match app.set_fault(name, None) {
                Ok(config) => {
                    protocol::admin_json(200, &serde_json::to_value(config).expect("config"))
                }
                Err(message) => error(400, &message),
            }
        }
        (Method::GET, [records]) if records == "records" => {
            let params = query_params(&query);
            let filter = match filter(params) {
                Ok(filter) => filter,
                Err(message) => return error(400, &message),
            };
            let items: Vec<RequestRecord> = app.records.snapshot(&filter);
            protocol::admin_json(200, &json!({"records": items, "count": items.len()}))
        }
        (Method::DELETE, [records]) if records == "records" => {
            let params = query_params(&query);
            let server = params.get("server").map(String::as_str);
            let cleared = app.records.clear(server);
            protocol::admin_json(200, &json!({"cleared": cleared}))
        }
        (Method::GET, [recording]) if recording == "recording" => {
            protocol::admin_json(200, &json!({"enabled": app.records.enabled()}))
        }
        (Method::PUT, [recording]) if recording == "recording" => {
            let set = match body::<SetRecording>(request, app).await {
                Ok(set) => set,
                Err(response) => return response,
            };
            app.records.set_enabled(set.enabled);
            protocol::admin_json(200, &json!({"enabled": app.records.enabled()}))
        }
        _ => error(404, "not found"),
    }
}

fn list_servers(app: &App) -> Response<Body> {
    let servers: Vec<Value> = app
        .instance_configs()
        .into_iter()
        .map(|config| serde_json::to_value(config).expect("config"))
        .collect();
    protocol::admin_json(200, &json!({"servers": servers}))
}

#[allow(clippy::result_large_err)]
async fn raw_body(request: Request<Incoming>, app: &App) -> Result<Value, Response<Body>> {
    let (_, body) = request.into_parts();
    let bytes = match Limited::new(body, app.options.max_body_bytes)
        .collect()
        .await
    {
        Ok(collected) => collected.to_bytes(),
        Err(_) => return Err(error(413, "request body too large")),
    };
    if bytes.is_empty() {
        return Ok(Value::Null);
    }
    serde_json::from_slice::<Value>(&bytes).map_err(|_| error(400, "invalid JSON"))
}

#[allow(clippy::result_large_err)]
async fn body<T: DeserializeOwned>(
    request: Request<Incoming>,
    app: &App,
) -> Result<T, Response<Body>> {
    let value = raw_body(request, app).await?;
    serde_json::from_value::<T>(value).map_err(|err| error(400, &format!("invalid body: {err}")))
}

fn error(status: u16, message: &str) -> Response<Body> {
    protocol::admin_json(status, &json!({"error": message}))
}

fn decode_segment(segment: &str) -> String {
    percent_decode_str(segment).decode_utf8_lossy().into_owned()
}

fn query_params(query: &str) -> HashMap<String, String> {
    url::form_urlencoded::parse(query.as_bytes())
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect()
}

fn filter(params: HashMap<String, String>) -> Result<RecordFilter, String> {
    let mut filter = RecordFilter {
        server: params.get("server").cloned(),
        http_method: params.get("http_method").cloned(),
        method: params.get("method").cloned(),
        tool: params.get("tool").cloned(),
        after: None,
        limit: None,
    };
    if let Some(value) = params.get("after") {
        filter.after = Some(
            value
                .parse()
                .map_err(|_| "after must be a non-negative integer".to_string())?,
        );
    }
    if let Some(value) = params.get("limit") {
        filter.limit = Some(
            value
                .parse()
                .map_err(|_| "limit must be a positive integer".to_string())?,
        );
    }
    Ok(filter)
}

#[allow(dead_code)]
fn unused(_: Bytes) {}
