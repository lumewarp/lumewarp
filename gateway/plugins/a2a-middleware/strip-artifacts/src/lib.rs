//! `strip-artifacts`: remove artifact parts whose media type the operator does
//! not want to reach the client.
//!
//! A2A v1.0 carries `Part`s on artifacts; this middleware drops the parts whose
//! `mediaType` is listed in `media_types` from
//!
//! - `Task.artifacts` in `SendMessage`, `GetTask`, `CancelTask` and `ListTasks`
//!   responses,
//! - `TaskArtifactUpdateEvent.artifact` in SSE streams.
//!
//! An artifact whose `parts` would become empty is removed from the task, and a
//! `TaskArtifactUpdateEvent` with no parts left is dropped entirely (the only
//! event the gateway lets a middleware drop). Every other event, and the
//! request, passes through unchanged.
//!
//! The plugin only sees what the gateway hands it: upstream ids (the gateway
//! wraps them afterwards), never client credentials.

use lumewarp_plugin_sdk::{
    middleware_types, register, ConfigError, ConfigureMiddleware, Middleware,
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::Value;

/// Most media types one agent may configure.
pub const MAX_MEDIA_TYPES: usize = 32;
/// Longest accepted media type.
pub const MAX_MEDIA_TYPE_LEN: usize = 128;

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Media types to remove; `mediaType` is compared exactly.
    #[serde(default)]
    #[schemars(title = "Media types")]
    pub media_types: Vec<String>,
}

pub struct StripArtifacts {
    media_types: Vec<String>,
}

impl StripArtifacts {
    fn strips(&self, part: &Value) -> bool {
        part.get("mediaType")
            .and_then(Value::as_str)
            .is_some_and(|media_type| self.media_types.iter().any(|m| m == media_type))
    }

    /// Rewrites one `Task` in place.
    fn strip_artifacts(&self, task: &mut Value) {
        strip_task(task, |part| self.strips(part));
    }

    /// One `result` of a JSON-RPC response. Ids are the upstream ones; this
    /// function never touches them.
    fn strip_result(&self, result: &mut Value) {
        // GetTask / CancelTask: the Task is the result itself.
        if result.get("status").is_some() && result.get("id").is_some() {
            strip_task(result, |part| self.strips(part));
        }
        let Some(object) = result.as_object_mut() else {
            return;
        };
        // SendMessageResponse: {task} or {message}; StreamResponse: oneof.
        if let Some(task) = object.get_mut("task") {
            strip_task(task, |part| self.strips(part));
        }
        if let Some(artifact_update) = object.get_mut("artifactUpdate") {
            self.strip_update(artifact_update);
        }
        // ListTasks: {tasks: [Task], ...}.
        if let Some(tasks) = object.get_mut("tasks").and_then(Value::as_array_mut) {
            for task in tasks {
                strip_task(task, |part| self.strips(part));
            }
        }
    }

    /// A `TaskArtifactUpdateEvent`. `parts` empty ⇒ every part was stripped.
    fn strip_update(&self, update: &mut Value) {
        let Some(artifact) = update.get_mut("artifact") else {
            return;
        };
        strip_artifact(artifact, |part| self.strips(part));
    }
}

/// Removes matching parts from every artifact of one `Task`.
fn strip_task(task: &mut Value, strips: impl Fn(&Value) -> bool) {
    let Some(artifacts) = task.get_mut("artifacts").and_then(Value::as_array_mut) else {
        return;
    };
    artifacts.retain_mut(|artifact| strip_artifact(artifact, &strips));
}

/// Removes matching parts from one artifact and reports whether any remain.
fn strip_artifact(artifact: &mut Value, strips: impl Fn(&Value) -> bool) -> bool {
    let Some(parts) = artifact.get_mut("parts").and_then(Value::as_array_mut) else {
        return true;
    };
    parts.retain(|part| !strips(part));
    !parts.is_empty()
}

impl ConfigureMiddleware<Params> for StripArtifacts {
    fn configure(_: &middleware_types::AgentInfo, params: Params) -> Result<Self, ConfigError> {
        if params.media_types.len() > MAX_MEDIA_TYPES {
            return Err(ConfigError(format!(
                "at most {MAX_MEDIA_TYPES} media types"
            )));
        }
        for media_type in &params.media_types {
            if media_type.is_empty() || media_type.len() > MAX_MEDIA_TYPE_LEN {
                return Err(ConfigError(format!(
                    "media type must be 1..={MAX_MEDIA_TYPE_LEN} bytes"
                )));
            }
        }
        Ok(Self {
            media_types: params.media_types,
        })
    }
}

impl Middleware for StripArtifacts {
    fn on_request(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        Ok(body)
    }

    fn on_response(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        let Some(mut value) = parse(&body) else {
            return Ok(body);
        };
        if let Some(result) = value.get_mut("result") {
            self.strip_result(result);
        }
        Ok(serde_json::to_vec(&value).unwrap_or(body))
    }

    fn on_stream_event(
        &self,
        _: String,
        data: Vec<u8>,
    ) -> Result<Option<Vec<u8>>, middleware_types::RpcError> {
        let Some(mut value) = parse(&data) else {
            return Ok(Some(data));
        };
        let mut drop = false;
        if let Some(result) = value.get_mut("result").and_then(Value::as_object_mut) {
            if let Some(update) = result.get_mut("artifactUpdate") {
                // Drop the event when the whole artifact disappears: a
                // `TaskArtifactUpdateEvent` with no `parts` is invalid.
                drop = {
                    let Some(artifact) = update.get_mut("artifact") else {
                        return Ok(Some(data));
                    };
                    !strip_artifact(artifact, |part| self.strips(part))
                };
            } else if let Some(task) = result.get_mut("task") {
                self.strip_artifacts(task);
            }
        }
        if drop {
            return Ok(None);
        }
        Ok(Some(serde_json::to_vec(&value).unwrap_or(data)))
    }
}

fn parse(bytes: &[u8]) -> Option<Value> {
    let value: Value = serde_json::from_slice(bytes).ok()?;
    value.is_object().then_some(value)
}

register! {
    kind: "a2a-middleware",
    name: "strip_artifacts",
    title: "Strip artifacts",
    description: "Removes artifact parts with the configured media types from responses and streams.",
    params: Params,
    middleware: StripArtifacts,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn plugin() -> StripArtifacts {
        StripArtifacts {
            media_types: vec!["application/x-secret".into()],
        }
    }

    #[test]
    fn response_parts_are_removed_and_empty_artifacts_disappear() {
        let body = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"task": {
                "id": "t1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"artifactId": "a1", "parts": [
                        {"text": "keep"},
                        {"raw": "c2VjcmV0", "mediaType": "application/x-secret"},
                    ]},
                    {"artifactId": "a2", "parts": [
                        {"raw": "c2VjcmV0", "mediaType": "application/x-secret"},
                    ]},
                ],
            }},
        });
        let output = plugin()
            .on_response(String::new(), serde_json::to_vec(&body).unwrap())
            .unwrap();
        let output: Value = serde_json::from_slice(&output).unwrap();
        let artifacts = output["result"]["task"]["artifacts"].as_array().unwrap();
        assert_eq!(artifacts.len(), 1);
        assert_eq!(artifacts[0]["artifactId"], "a1");
        assert_eq!(artifacts[0]["parts"].as_array().unwrap().len(), 1);
        assert_eq!(artifacts[0]["parts"][0]["text"], "keep");
    }

    #[test]
    fn a_get_task_result_and_list_tasks_are_stripped_too() {
        let task = json!({
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "a1", "parts": [
                {"text": "keep"},
                {"raw": "c2VjcmV0", "mediaType": "application/x-secret"},
            ]}],
        });
        let plugin = plugin();
        let mut task_result = task.clone();
        plugin.strip_result(&mut task_result);
        assert_eq!(
            task_result["artifacts"][0]["parts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let mut list_result = json!({"tasks": [task], "nextPageToken": ""});
        plugin.strip_result(&mut list_result);
        assert_eq!(
            list_result["tasks"][0]["artifacts"][0]["parts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn a_fully_stripped_artifact_event_is_dropped() {
        let data = json!({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"artifactUpdate": {
                "taskId": "t1",
                "contextId": "c1",
                "artifact": {"artifactId": "a1", "parts": [
                    {"raw": "c2VjcmV0", "mediaType": "application/x-secret"},
                ]},
            }},
        });
        let output = plugin()
            .on_stream_event(String::new(), serde_json::to_vec(&data).unwrap())
            .unwrap();
        assert!(output.is_none());
    }

    #[test]
    fn a_partially_stripped_artifact_event_survives() {
        let data = json!({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"artifactUpdate": {
                "taskId": "t1",
                "contextId": "c1",
                "artifact": {"artifactId": "a1", "parts": [
                    {"text": "keep"},
                    {"raw": "c2VjcmV0", "mediaType": "application/x-secret"},
                ]},
            }},
        });
        let output = plugin()
            .on_stream_event(String::new(), serde_json::to_vec(&data).unwrap())
            .unwrap()
            .expect("event survives");
        let output: Value = serde_json::from_slice(&output).unwrap();
        let parts = output["result"]["artifactUpdate"]["artifact"]["parts"]
            .as_array()
            .unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0]["text"], "keep");
    }

    #[test]
    fn status_updates_and_requests_pass_through() {
        let update = json!({"jsonrpc": "2.0", "id": 2, "result": {"statusUpdate": {
            "taskId": "t1", "contextId": "c1",
            "status": {"state": "TASK_STATE_COMPLETED"},
        }}});
        let bytes = serde_json::to_vec(&update).unwrap();
        assert_eq!(
            plugin()
                .on_stream_event(String::new(), bytes.clone())
                .unwrap(),
            Some(bytes.clone())
        );
        assert_eq!(
            plugin().on_request(String::new(), bytes.clone()).unwrap(),
            bytes
        );
    }

    #[test]
    fn media_types_are_compared_exactly_and_bounded() {
        let plugin = StripArtifacts {
            media_types: vec!["text/plain".into()],
        };
        assert!(!plugin.strips(&json!({"mediaType": "text/plain; charset=utf-8"})));
        assert!(plugin.strips(&json!({"mediaType": "text/plain"})));
        assert!(!plugin.strips(&json!({"text": "no media type"})));
        let agent = middleware_types::AgentInfo {
            name: "a".into(),
            url: "http://127.0.0.1:1/a2a".into(),
            params_json: "{}".into(),
        };
        assert!(StripArtifacts::configure(
            &agent,
            Params {
                media_types: vec![String::new()]
            }
        )
        .is_err());
        assert!(StripArtifacts::configure(
            &agent,
            Params {
                media_types: vec!["x".repeat(MAX_MEDIA_TYPE_LEN + 1)]
            }
        )
        .is_err());
        assert!(StripArtifacts::configure(
            &agent,
            Params {
                media_types: (0..=MAX_MEDIA_TYPES).map(|_| "a/b".into()).collect()
            }
        )
        .is_err());
    }
}
