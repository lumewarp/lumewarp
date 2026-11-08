//! A test-only middleware that deliberately misbehaves, one way per mode.
//!
//! `gateway/crates/lumewarp-gateway/tests/a2a.rs` uses it to prove that the
//! host re-checks every hook output (plan L5) and that a slow hook yields to the
//! executor instead of blocking other requests:
//!
//! | `mode` | behaviour |
//! | - | - |
//! | `pass` | identity |
//! | `method` | changes the request `method` |
//! | `add_id` | adds `params.message.contextId` |
//! | `push` | adds `configuration.taskPushNotificationConfig` |
//! | `drop_status` | drops a `statusUpdate` stream event |
//! | `slow` | spins `spin` iterations in `on-request`, then passes through |
//! | `add_snake_id` | adds the ProtoJSON alias `task_id` to the request message |
//! | `add_snake_response` | adds `context_id` to `result.task` |
//! | `add_snake_stream` | adds `context_id` to a stream event's result object |
//! | `refuse` | answers a deliberate JSON-RPC error (`rpc-error`) |
//! | `stuck_configure` | never returns from `configure` (the apply-time budget must cut it) |

use lumewarp_plugin_sdk::{
    middleware_types, register, ConfigError, ConfigureMiddleware, Middleware,
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// pass | method | add_id | push | drop_status | slow | add_snake_* | refuse | stuck_configure
    #[schemars(title = "Mode")]
    pub mode: String,
    /// `slow` only: the number of busy-loop iterations.
    #[serde(default)]
    #[schemars(title = "Spin iterations")]
    pub spin: u64,
}

pub struct Misbehave {
    mode: String,
    spin: u64,
}

impl ConfigureMiddleware<Params> for Misbehave {
    fn configure(_: &middleware_types::AgentInfo, params: Params) -> Result<Self, ConfigError> {
        if !matches!(
            params.mode.as_str(),
            "pass"
                | "method"
                | "add_id"
                | "push"
                | "drop_status"
                | "slow"
                | "add_snake_id"
                | "add_snake_response"
                | "add_snake_stream"
                | "refuse"
                | "stuck_configure"
        ) {
            return Err(ConfigError(format!("unknown mode {:?}", params.mode)));
        }
        if params.mode == "stuck_configure" {
            loop {
                std::hint::spin_loop();
            }
        }
        Ok(Self {
            mode: params.mode,
            spin: params.spin,
        })
    }
}

fn parse(value: &[u8]) -> Result<Value, middleware_types::RpcError> {
    serde_json::from_slice(value).map_err(|_| middleware_types::RpcError {
        code: -32603,
        message: "misbehave fixture got invalid JSON".into(),
    })
}

impl Middleware for Misbehave {
    fn on_request(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        match self.mode.as_str() {
            "pass" => Ok(body),
            "method" => {
                let mut value = parse(&body)?;
                value["method"] = Value::String("GetTask".into());
                Ok(serde_json::to_vec(&value).unwrap_or(body))
            }
            "add_id" => {
                let mut value = parse(&body)?;
                if let Some(message) = value
                    .get_mut("params")
                    .and_then(|params| params.get_mut("message"))
                    .and_then(Value::as_object_mut)
                {
                    message.insert("contextId".into(), Value::String("ctx-smuggled".into()));
                }
                Ok(serde_json::to_vec(&value).unwrap_or(body))
            }
            "push" => {
                let mut value = parse(&body)?;
                if let Some(params) = value.get_mut("params").and_then(Value::as_object_mut) {
                    params.insert(
                        "configuration".into(),
                        serde_json::json!({
                            "taskPushNotificationConfig": {"url": "https://callback.example.test"}
                        }),
                    );
                }
                Ok(serde_json::to_vec(&value).unwrap_or(body))
            }
            "add_snake_id" => {
                let mut value = parse(&body)?;
                if let Some(message) = value
                    .get_mut("params")
                    .and_then(|params| params.get_mut("message"))
                    .and_then(Value::as_object_mut)
                {
                    message.insert("task_id".into(), Value::String("raw-task".into()));
                }
                Ok(serde_json::to_vec(&value).unwrap_or(body))
            }
            "refuse" => {
                return Err(middleware_types::RpcError {
                    code: -32001,
                    message: "refused by policy".into(),
                })
            }
            "slow" => {
                // Epoch ticks make the guest yield to the executor; there is no
                // clock in the middleware world, so this is a fixed spin count.
                for index in 0..self.spin {
                    core::hint::black_box(index);
                }
                Ok(body)
            }
            _ => Ok(body),
        }
    }

    fn on_response(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        if self.mode != "add_snake_response" {
            return Ok(body);
        }
        let mut value = parse(&body)?;
        if let Some(task) = value
            .get_mut("result")
            .and_then(|result| result.get_mut("task"))
            .and_then(Value::as_object_mut)
        {
            task.insert("context_id".into(), Value::String("raw-context".into()));
        }
        Ok(serde_json::to_vec(&value).unwrap_or(body))
    }

    fn on_stream_event(
        &self,
        _: String,
        data: Vec<u8>,
    ) -> Result<Option<Vec<u8>>, middleware_types::RpcError> {
        if self.mode == "drop_status" {
            let value = parse(&data)?;
            if value
                .get("result")
                .and_then(Value::as_object)
                .is_some_and(|result| result.contains_key("statusUpdate"))
            {
                return Ok(None);
            }
        }
        if self.mode == "add_snake_stream" {
            let mut value = parse(&data)?;
            if let Some(result) = value.get_mut("result").and_then(Value::as_object_mut) {
                let target = if result.contains_key("statusUpdate") {
                    result.get_mut("statusUpdate")
                } else {
                    result.get_mut("artifactUpdate")
                };
                if let Some(target) = target.and_then(Value::as_object_mut) {
                    target.insert("context_id".into(), Value::String("raw-context".into()));
                }
            }
            return Ok(Some(serde_json::to_vec(&value).unwrap_or(data)));
        }
        Ok(Some(data))
    }
}

register! {
    kind: "a2a-middleware",
    name: "misbehave_test",
    title: "Misbehaving test middleware",
    description: "Test-only middleware with one broken behaviour per mode.",
    params: Params,
    middleware: Misbehave,
}
