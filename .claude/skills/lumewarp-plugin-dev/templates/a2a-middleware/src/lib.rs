//! `my_middleware`: stamps a fixed key and value into the `metadata` of every
//! `SendMessage` and `SendStreamingMessage` request, and passes responses and
//! stream events through.
//!
//! A middleware sees the JSON-RPC method and body only, with upstream task and
//! context ids. The gateway re-checks every result: changing the method, adding
//! an id or adding push-notification configuration is refused.

use lumewarp_plugin_sdk::{
    middleware_types, register, serde_json, ConfigError, ConfigureMiddleware, Middleware,
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::Value;

/// JSON-RPC "Invalid params".
const INVALID_PARAMS: i32 = -32602;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Metadata key to set on each message request.
    #[schemars(title = "Metadata key")]
    pub key: String,
    #[schemars(title = "Metadata value")]
    pub value: String,
}

pub struct MyMiddleware {
    key: String,
    value: String,
}

impl ConfigureMiddleware<Params> for MyMiddleware {
    fn configure(_: &middleware_types::AgentInfo, params: Params) -> Result<Self, ConfigError> {
        if params.key.is_empty() || params.key.len() > 128 {
            return Err(ConfigError("key must be 1..=128 bytes".into()));
        }
        Ok(Self {
            key: params.key,
            value: params.value,
        })
    }
}

impl MyMiddleware {
    fn stamp(&self, method: &str, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        if method != "SendMessage" && method != "SendStreamingMessage" {
            return Ok(body);
        }
        let mut request: Value = serde_json::from_slice(&body).map_err(|_| invalid("body"))?;
        let params = request
            .get_mut("params")
            .and_then(Value::as_object_mut)
            .ok_or_else(|| invalid("params"))?;
        let metadata = params
            .entry("metadata")
            .or_insert_with(|| Value::Object(Default::default()));
        let metadata = metadata
            .as_object_mut()
            .ok_or_else(|| invalid("metadata"))?;
        metadata.insert(self.key.clone(), Value::String(self.value.clone()));
        serde_json::to_vec(&request).map_err(|_| invalid("body"))
    }
}

fn invalid(what: &str) -> middleware_types::RpcError {
    middleware_types::RpcError {
        code: INVALID_PARAMS,
        message: format!("invalid {what}"),
    }
}

impl Middleware for MyMiddleware {
    fn on_request(
        &self,
        method: String,
        body: Vec<u8>,
    ) -> Result<Vec<u8>, middleware_types::RpcError> {
        self.stamp(&method, body)
    }

    fn on_response(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        Ok(body)
    }

    /// Returning `None` drops the event; only a `TaskArtifactUpdateEvent` may
    /// be dropped.
    fn on_stream_event(
        &self,
        _: String,
        data: Vec<u8>,
    ) -> Result<Option<Vec<u8>>, middleware_types::RpcError> {
        Ok(Some(data))
    }
}

register! {
    kind: "a2a-middleware",
    name: "my_middleware",
    title: "Stamp metadata",
    description: "Set a fixed metadata key and value on every message request.",
    params: Params,
    middleware: MyMiddleware,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn middleware() -> MyMiddleware {
        MyMiddleware {
            key: "tenant".into(),
            value: "blue".into(),
        }
    }

    fn json(bytes: &[u8]) -> Value {
        serde_json::from_slice(bytes).unwrap()
    }

    #[test]
    fn message_requests_get_the_metadata() {
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m"}}}"#;
        let out = middleware().stamp("SendMessage", body.to_vec()).unwrap();
        assert_eq!(json(&out)["params"]["metadata"]["tenant"], "blue");
        assert_eq!(
            json(&out)["params"]["message"],
            json(body)["params"]["message"]
        );
    }

    #[test]
    fn existing_metadata_is_kept() {
        let body =
            br#"{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"metadata":{"a":1}}}"#;
        let out = middleware().stamp("SendMessage", body.to_vec()).unwrap();
        assert_eq!(json(&out)["params"]["metadata"]["a"], 1);
    }

    #[test]
    fn other_methods_pass_through_untouched() {
        let body = br#"{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"t"}}"#;
        assert_eq!(middleware().stamp("GetTask", body.to_vec()).unwrap(), body);
    }

    #[test]
    fn a_malformed_request_is_a_json_rpc_error() {
        let error = middleware()
            .stamp("SendMessage", b"{".to_vec())
            .unwrap_err();
        assert_eq!(error.code, INVALID_PARAMS);
    }
}
