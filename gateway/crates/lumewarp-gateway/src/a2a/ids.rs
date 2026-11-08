//! Task and context id ownership for the A2A proxy (plan P3, D17, L5).
//!
//! The gateway is stateless: every task/context id a client sees is
//! `lwt.<key_id>.<upstream id>.<mac>` from [`crate::a2a_ids`]. Requests are
//! unwrapped before they reach the middleware or the upstream, responses and
//! every SSE event are wrapped after the response hook, so a hook only ever
//! sees upstream ids and a client only ever sees ids minted for its own key.
//!
//! Only the typed v1.0 fields are touched (the oracle's review of P3):
//!
//! | Direction | Fields |
//! | - | - |
//! | request | `SendMessage`/`SendStreamingMessage`: `message.taskId`, `message.contextId`, `message.referenceTaskIds[]`; `GetTask`/`CancelTask`/`SubscribeToTask`: `params.id`; `ListTasks`: `params.contextId` |
//! | response | `Task.id`, `Task.contextId`, every `Message` (`taskId`, `contextId`, `referenceTaskIds[]`), `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`, `ListTasks.tasks[]`, `error.data[].metadata` |
//!
//! `messageId`, `artifactId`, skill ids and JSON-RPC ids are identities their
//! creators chose; they are never rewritten. A failed unwrap is a
//! task-not-found error for both "another key's id" and "not one of ours", so
//! the gateway never reveals whether an id exists.

use serde_json::Value;

use crate::a2a_ids::{IdKey, UnwrapError};

/// `TaskNotFoundError` (`ref/specs.md` §2.8).
pub const TASK_NOT_FOUND: i32 = -32001;

/// Who an id belongs to: one agent and one API key (plan D17).
#[derive(Clone, Copy)]
pub struct Context<'a> {
    pub key: &'a IdKey,
    pub agent: &'a str,
    /// The caller's `key_id`.
    pub owner: &'a str,
}

/// Why a client id could not be accepted. Both variants answer task-not-found;
/// the difference is only for spans and tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnwrapFailure {
    /// Not minted by this gateway (raw, tampered with, or another agent's).
    NotOurs,
    /// A genuine id minted for another key.
    Foreign,
}

impl From<UnwrapError> for UnwrapFailure {
    fn from(error: UnwrapError) -> Self {
        match error {
            UnwrapError::NotOurs => Self::NotOurs,
            UnwrapError::OtherOwner => Self::Foreign,
        }
    }
}

/// An upstream id the gateway cannot wrap (empty, oversized or non-UTF-8). The
/// response cannot be completed safely; the proxy answers
/// `InvalidAgentResponseError`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WrapFailure;

fn unwrap_value(value: &mut Value, ctx: &Context<'_>) -> Result<(), UnwrapFailure> {
    match value {
        // ProtoJSON omits unset optionals; an explicit null is treated the same.
        Value::Null => Ok(()),
        Value::String(text) => {
            *text = ctx
                .key
                .unwrap(ctx.agent, ctx.owner, text)
                .map_err(UnwrapFailure::from)?;
            Ok(())
        }
        // A non-string id is client input the upstream cannot own either.
        _ => Err(UnwrapFailure::NotOurs),
    }
}

fn unwrap_list(value: &mut Value, ctx: &Context<'_>) -> Result<(), UnwrapFailure> {
    match value {
        Value::Null => Ok(()),
        Value::Array(items) => {
            for item in items {
                unwrap_value(item, ctx)?;
            }
            Ok(())
        }
        _ => Err(UnwrapFailure::NotOurs),
    }
}

fn unwrap_message(message: &mut Value, ctx: &Context<'_>) -> Result<(), UnwrapFailure> {
    let Some(object) = message.as_object_mut() else {
        return Err(UnwrapFailure::NotOurs);
    };
    for field in ["taskId", "contextId"] {
        if let Some(value) = object.get_mut(field) {
            unwrap_value(value, ctx)?;
        }
    }
    if let Some(value) = object.get_mut("referenceTaskIds") {
        unwrap_list(value, ctx)?;
    }
    Ok(())
}

/// Rewrites the id-bearing fields of one JSON-RPC request in place, replacing
/// wrapped ids with the upstream ids the agent minted.
pub fn unwrap_request(
    method: &str,
    body: &mut Value,
    ctx: &Context<'_>,
) -> Result<(), UnwrapFailure> {
    let Some(params) = body.get_mut("params") else {
        return Ok(());
    };
    let Some(object) = params.as_object_mut() else {
        // Parameter shape is validated before this; leave malformed input for
        // the upstream's InvalidParams answer.
        return Ok(());
    };
    match method {
        "SendMessage" | "SendStreamingMessage" => {
            if let Some(message) = object.get_mut("message") {
                unwrap_message(message, ctx)?;
            }
        }
        "GetTask" | "CancelTask" | "SubscribeToTask" => {
            if let Some(value) = object.get_mut("id") {
                unwrap_value(value, ctx)?;
            }
        }
        "ListTasks" => {
            if let Some(value) = object.get_mut("contextId") {
                unwrap_value(value, ctx)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn wrap_string(value: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Value::String(text) = value else {
        return Ok(());
    };
    *text = ctx
        .key
        .wrap(ctx.agent, ctx.owner, text)
        .ok_or(WrapFailure)?;
    Ok(())
}

fn wrap_list(value: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Value::Array(items) = value else {
        return Ok(());
    };
    for item in items {
        wrap_string(item, ctx)?;
    }
    Ok(())
}

fn wrap_message(message: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Some(object) = message.as_object_mut() else {
        return Ok(());
    };
    for field in ["taskId", "contextId"] {
        if let Some(value) = object.get_mut(field) {
            wrap_string(value, ctx)?;
        }
    }
    if let Some(value) = object.get_mut("referenceTaskIds") {
        wrap_list(value, ctx)?;
    }
    Ok(())
}

fn wrap_status(status: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    if let Some(message) = status.get_mut("message") {
        wrap_message(message, ctx)?;
    }
    Ok(())
}

fn wrap_task(task: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Some(object) = task.as_object_mut() else {
        return Ok(());
    };
    for field in ["id", "contextId"] {
        if let Some(value) = object.get_mut(field) {
            wrap_string(value, ctx)?;
        }
    }
    if let Some(status) = object.get_mut("status") {
        wrap_status(status, ctx)?;
    }
    if let Some(history) = object.get_mut("history").and_then(Value::as_array_mut) {
        for message in history {
            wrap_message(message, ctx)?;
        }
    }
    Ok(())
}

fn wrap_status_update(update: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Some(object) = update.as_object_mut() else {
        return Ok(());
    };
    for field in ["taskId", "contextId"] {
        if let Some(value) = object.get_mut(field) {
            wrap_string(value, ctx)?;
        }
    }
    if let Some(status) = object.get_mut("status") {
        wrap_status(status, ctx)?;
    }
    Ok(())
}

fn wrap_artifact_update(update: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    let Some(object) = update.as_object_mut() else {
        return Ok(());
    };
    for field in ["taskId", "contextId"] {
        if let Some(value) = object.get_mut(field) {
            wrap_string(value, ctx)?;
        }
    }
    Ok(())
}

/// A typed `error.data` array: `google.rpc.ErrorInfo.metadata` may echo the raw
/// task or context id. Rewrite the two known keys; anything else is left alone,
/// and an unrepresentable id is dropped rather than leaked.
fn wrap_error_data(error: &mut Value, ctx: &Context<'_>) {
    let Some(items) = error.get_mut("data").and_then(Value::as_array_mut) else {
        return;
    };
    for item in items {
        let Some(metadata) = item.get_mut("metadata").and_then(Value::as_object_mut) else {
            continue;
        };
        for field in ["taskId", "contextId"] {
            if let Some(value) = metadata.get_mut(field) {
                let Value::String(text) = value else {
                    continue;
                };
                match ctx.key.wrap(ctx.agent, ctx.owner, text) {
                    Some(wrapped) => *text = wrapped,
                    None => {
                        metadata.remove(field);
                    }
                }
            }
        }
    }
}

/// Shape-based rewriting of one `result`: `Task`, `SendMessageResponse`,
/// `StreamResponse`, `ListTasksResponse`, or an Agent Card (no ids).
pub fn wrap_result(method: &str, result: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    // `GetExtendedAgentCard` returns an AgentCard; it has no task/context ids.
    if method == "GetExtendedAgentCard" {
        return Ok(());
    }
    let Some(object) = result.as_object() else {
        return Ok(());
    };
    // A bare Task (GetTask, CancelTask).
    if object.contains_key("status") && object.contains_key("id") {
        return wrap_task(result, ctx);
    }
    if object.contains_key("tasks") {
        if let Some(tasks) = result
            .get_mut("tasks")
            .and_then(serde_json::Value::as_array_mut)
        {
            for task in tasks {
                wrap_task(task, ctx)?;
            }
        }
        return Ok(());
    }
    if let Some(task) = result.get_mut("task") {
        wrap_task(task, ctx)?;
    }
    if let Some(message) = result.get_mut("message") {
        wrap_message(message, ctx)?;
    }
    if let Some(update) = result.get_mut("statusUpdate") {
        wrap_status_update(update, ctx)?;
    }
    if let Some(update) = result.get_mut("artifactUpdate") {
        wrap_artifact_update(update, ctx)?;
    }
    Ok(())
}

/// Rewrites the `result` and `error` of one non-streaming JSON-RPC response (or
/// one SSE event's JSON-RPC response object).
pub fn wrap_response(method: &str, body: &mut Value, ctx: &Context<'_>) -> Result<(), WrapFailure> {
    if let Some(result) = body.get_mut("result") {
        wrap_result(method, result, ctx)?;
    }
    if let Some(error) = body.get_mut("error") {
        wrap_error_data(error, ctx);
    }
    Ok(())
}

/// ProtoJSON accepts the original proto field names as aliases for the
/// lowerCamelCase JSON names. The gateway's id and push handling only knows the
/// canonical names, so an alias would bypass unwrapping (raw ids reach the
/// agent and the response is wrapped as the caller's), the D12 refusal, and the
/// response-time wrapping. These are the aliases of every id- or push-bearing
/// field this module handles; `task_push_notification_config` is D12's field.
pub const SNAKE_ALIASES: [&str; 4] = [
    "task_id",
    "context_id",
    "reference_task_ids",
    "task_push_notification_config",
];

fn is_snake_alias(key: &str) -> bool {
    SNAKE_ALIASES.contains(&key)
}

/// The first snake_case alias in `value`, with its JSON path. `metadata` and
/// `Part.data` hold arbitrary JSON, so keys there are not typed fields and are
/// skipped; [`response_snake_alias`] checks the camelCase-id positions inside
/// `error.data[].metadata` separately.
pub fn snake_alias(value: &Value) -> Option<String> {
    fn walk(value: &Value, path: &str, in_metadata: bool) -> Option<String> {
        match value {
            Value::Object(object) => {
                for (key, child) in object {
                    let child_path = format!("{path}.{key}");
                    if !in_metadata && is_snake_alias(key) {
                        return Some(child_path);
                    }
                    let opaque = in_metadata || key == "metadata" || key == "data";
                    if let Some(found) = walk(child, &child_path, opaque) {
                        return Some(found);
                    }
                }
                None
            }
            Value::Array(items) => {
                for (index, item) in items.iter().enumerate() {
                    if let Some(found) = walk(item, &format!("{path}[{index}]"), in_metadata) {
                        return Some(found);
                    }
                }
                None
            }
            _ => None,
        }
    }
    walk(value, "$", false)
}

/// The snake_case aliases of the id fields a request's `params` may carry. The
/// request hook's output is checked with this too.
pub fn request_snake_alias(body: &Value) -> Option<String> {
    body.get("params")
        .and_then(snake_alias)
        .map(|path| format!("params{}", path.trim_start_matches('$')))
}

/// The snake_case aliases of the id fields a response may carry: the result
/// (a `Task`, message, status/artifact event or `ListTasks` page) and the
/// typed `error.data[].metadata` positions that `wrap_error_data` would wrap.
/// Used before wrapping and on response/stream hook output.
pub fn response_snake_alias(body: &Value) -> Option<String> {
    if let Some(result) = body.get("result") {
        if let Some(found) = snake_alias(result) {
            return Some(format!("result{}", found.trim_start_matches('$')));
        }
    }
    if let Some(error) = body.get("error") {
        if let Some(items) = error.get("data").and_then(Value::as_array) {
            for (index, item) in items.iter().enumerate() {
                let Some(metadata) = item.get("metadata").and_then(Value::as_object) else {
                    continue;
                };
                for field in ["task_id", "context_id"] {
                    if metadata.contains_key(field) {
                        return Some(format!("error.data[{index}].metadata.{field}"));
                    }
                }
            }
        }
        if let Some(found) = snake_alias(error) {
            return Some(format!("error{}", found.trim_start_matches('$')));
        }
    }
    None
}

fn add_path(out: &mut Vec<(String, String)>, path: String, value: &Value) {
    if let Some(text) = value.as_str() {
        out.push((path, text.to_owned()));
    }
}

/// Every string under a `taskId`, `contextId` or `referenceTaskIds` key, with
/// its JSON path. Shared names are unambiguous in v1.0 typed objects.
fn collect_named(value: &Value, path: &str, out: &mut Vec<(String, String)>) {
    match value {
        Value::Object(object) => {
            for (key, child) in object {
                let child_path = format!("{path}.{key}");
                if key == "taskId" || key == "contextId" {
                    add_path(out, child_path, child);
                } else if key == "referenceTaskIds" {
                    if let Value::Array(items) = child {
                        for (index, item) in items.iter().enumerate() {
                            add_path(out, format!("{child_path}[{index}]"), item);
                        }
                    }
                } else {
                    collect_named(child, &child_path, out);
                }
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                collect_named(item, &format!("{path}[{index}]"), out);
            }
        }
        _ => {}
    }
}

/// The id-bearing fields of a request as the middleware sees it: the envelope
/// `id`, every named id field, and `params.id` for the task methods. A request
/// hook must return exactly this set with exactly these values.
pub fn request_fields(method: &str, body: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Some(id) = body.get("id") {
        if !id.is_null() {
            out.push(("$id".to_owned(), id.to_string()));
        }
    }
    let Some(params) = body.get("params") else {
        return out;
    };
    if matches!(method, "GetTask" | "CancelTask" | "SubscribeToTask") {
        if let Some(id) = params.get("id") {
            add_path(&mut out, "params.id".to_owned(), id);
        }
    }
    collect_named(params, "params", &mut out);
    out
}

/// The id-bearing fields of a response as the middleware sees it (upstream,
/// raw ids). A response hook may drop entries but must not add or change one.
pub fn response_fields(body: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Some(id) = body.get("id") {
        if !id.is_null() {
            out.push(("$id".to_owned(), id.to_string()));
        }
    }
    if let Some(result) = body.get("result") {
        if let Some(object) = result.as_object() {
            if object.contains_key("status") && object.contains_key("id") {
                add_path(&mut out, "result.id".to_owned(), &result["id"]);
            }
            if let Some(task) = object.get("task") {
                if let Some(id) = task.get("id") {
                    add_path(&mut out, "result.task.id".to_owned(), id);
                }
            }
            if let Some(tasks) = object.get("tasks").and_then(Value::as_array) {
                for (index, task) in tasks.iter().enumerate() {
                    if let Some(id) = task.get("id") {
                        add_path(&mut out, format!("result.tasks[{index}].id"), id);
                    }
                }
            }
        }
        collect_named(result, "result", &mut out);
    }
    // `wrap_error_data` rewrites typed error metadata, so the hook check must see
    // those fields too: a hook that adds a raw id there must not get it wrapped
    // as the caller's (m4).
    if let Some(error) = body.get("error") {
        collect_named(error, "error", &mut out);
    }
    out
}

/// Whether an answer to `method`/`params` would make the agent send push
/// notifications (plan D12). The four CRUD methods and an embedded
/// `configuration.taskPushNotificationConfig` are refused before the upstream
/// sees them, and refused again if a middleware adds one.
pub fn push_config_present(method: &str, params: Option<&Value>) -> bool {
    if matches!(
        method,
        "CreateTaskPushNotificationConfig"
            | "GetTaskPushNotificationConfig"
            | "ListTaskPushNotificationConfigs"
            | "DeleteTaskPushNotificationConfig"
    ) {
        return true;
    }
    params
        .and_then(|params| params.get("configuration"))
        .and_then(Value::as_object)
        .is_some_and(|configuration| configuration.contains_key("taskPushNotificationConfig"))
}

/// Whether a stream event's `result` is a `TaskArtifactUpdateEvent`, the only
/// event plan L5 lets a middleware drop.
pub fn is_artifact_update(body: &Value) -> bool {
    body.get("result")
        .and_then(Value::as_object)
        .is_some_and(|result| result.contains_key("artifactUpdate"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const AGENT: &str = "helper";
    const OWNER: &str = "key-a";

    fn key() -> IdKey {
        IdKey::from_secret(vec![9u8; 32]).unwrap()
    }

    fn context<'a>(key: &'a IdKey) -> Context<'a> {
        Context {
            key,
            agent: AGENT,
            owner: OWNER,
        }
    }

    fn wrapped(key: &IdKey, id: &str) -> String {
        key.wrap(AGENT, OWNER, id).unwrap()
    }

    #[test]
    fn send_message_ids_are_unwrapped_and_history_is_wrapped() {
        let key = key();
        let ctx = context(&key);
        let mut request = json!({
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {
                "messageId": "m1", "role": "ROLE_USER",
                "taskId": wrapped(&key, "task-1"),
                "contextId": wrapped(&key, "ctx-1"),
                "referenceTaskIds": [wrapped(&key, "task-0")],
                "parts": [{"text": "hi"}],
            }},
        });
        unwrap_request("SendMessage", &mut request, &ctx).unwrap();
        assert_eq!(request["params"]["message"]["taskId"], "task-1");
        assert_eq!(request["params"]["message"]["contextId"], "ctx-1");
        assert_eq!(
            request["params"]["message"]["referenceTaskIds"][0],
            "task-0"
        );
        assert_eq!(request["params"]["message"]["messageId"], "m1");
    }

    #[test]
    fn task_methods_unwrap_params_id_and_list_tasks_unwraps_context() {
        let key = key();
        let ctx = context(&key);
        for method in ["GetTask", "CancelTask", "SubscribeToTask"] {
            let mut request = json!({
                "jsonrpc": "2.0", "id": 2, "method": method,
                "params": {"id": wrapped(&key, "task-9")},
            });
            unwrap_request(method, &mut request, &ctx).unwrap();
            assert_eq!(request["params"]["id"], "task-9");
        }
        let mut list = json!({
            "jsonrpc": "2.0", "id": 3, "method": "ListTasks",
            "params": {"contextId": wrapped(&key, "ctx-9"), "pageSize": 10},
        });
        unwrap_request("ListTasks", &mut list, &ctx).unwrap();
        assert_eq!(list["params"]["contextId"], "ctx-9");
    }

    #[test]
    fn raw_tampered_and_foreign_ids_are_refused_without_revealing_which() {
        let key = key();
        let ctx = context(&key);
        let foreign_key = IdKey::from_secret(vec![7u8; 32]).unwrap();
        let foreign = foreign_key.wrap(AGENT, "key-b", "task-1").unwrap();
        // Flip a character in the MAC's first byte group: changing the last
        // base64url character can be a no-op because its low bits are padding.
        let mut tampered = wrapped(&key, "task-1");
        let at = tampered.rfind('.').unwrap() + 1;
        let current = tampered.as_bytes()[at];
        tampered.replace_range(at..at + 1, if current == b'A' { "B" } else { "A" });
        for value in ["task-1".to_owned(), tampered, foreign] {
            let mut request = json!({
                "jsonrpc": "2.0", "id": 4, "method": "GetTask",
                "params": {"id": value},
            });
            assert!(unwrap_request("GetTask", &mut request, &ctx).is_err());
        }
        // A non-string id is refused as well.
        let mut request = json!({
            "jsonrpc": "2.0", "id": 4, "method": "GetTask", "params": {"id": 7},
        });
        assert_eq!(
            unwrap_request("GetTask", &mut request, &ctx),
            Err(UnwrapFailure::NotOurs)
        );
    }

    #[test]
    fn a_task_response_wraps_every_message_and_event_shape() {
        let key = key();
        let ctx = context(&key);
        let expected_task = wrapped(&key, "task-1");
        let expected_context = wrapped(&key, "ctx-1");
        let message = |id: &str| {
            json!({
                "messageId": id, "role": "ROLE_AGENT", "parts": [{"text": "x"}],
                "taskId": "task-1", "contextId": "ctx-1",
                "referenceTaskIds": ["task-0"],
            })
        };
        let mut result = json!({
            "task": {
                "id": "task-1", "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_COMPLETED", "message": message("m-status")},
                "history": [message("m-history")],
                "artifacts": [{"artifactId": "a-1", "parts": [{"text": "kept"}]}],
            }
        });
        wrap_result("SendMessage", &mut result, &ctx).unwrap();
        let task = &result["task"];
        assert_eq!(task["id"], expected_task);
        assert_eq!(task["contextId"], expected_context);
        assert_eq!(task["status"]["message"]["taskId"], expected_task);
        assert_eq!(task["history"][0]["contextId"], expected_context);
        assert_eq!(
            task["history"][0]["referenceTaskIds"][0],
            wrapped(&key, "task-0")
        );
        // messageId and artifactId are not ownership tokens.
        assert_eq!(task["status"]["message"]["messageId"], "m-status");
        assert_eq!(task["artifacts"][0]["artifactId"], "a-1");
        assert_eq!(task["artifacts"][0]["parts"][0]["text"], "kept");

        let mut status_update = json!({"statusUpdate": {
            "taskId": "task-1", "contextId": "ctx-1",
            "status": {"state": "TASK_STATE_WORKING", "message": message("m-1")},
        }});
        wrap_result("SendStreamingMessage", &mut status_update, &ctx).unwrap();
        assert_eq!(status_update["statusUpdate"]["taskId"], expected_task);
        assert_eq!(status_update["statusUpdate"]["contextId"], expected_context);
        let mut artifact_update = json!({"artifactUpdate": {
            "taskId": "task-1", "contextId": "ctx-1",
            "artifact": {"artifactId": "a-1", "parts": [{"text": "x"}]},
        }});
        wrap_result("SendStreamingMessage", &mut artifact_update, &ctx).unwrap();
        assert_eq!(artifact_update["artifactUpdate"]["taskId"], expected_task);
        assert_eq!(
            artifact_update["artifactUpdate"]["contextId"],
            expected_context
        );
    }

    #[test]
    fn a_list_tasks_response_wraps_every_task() {
        let key = key();
        let ctx = context(&key);
        let mut result = json!({
            "tasks": [
                {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_WORKING"}},
                {"id": "task-2", "status": {"state": "TASK_STATE_COMPLETED"}},
            ],
            "nextPageToken": "",
            "pageSize": 2,
            "totalSize": 2,
        });
        wrap_result("ListTasks", &mut result, &ctx).unwrap();
        assert_eq!(result["tasks"][0]["id"], wrapped(&key, "task-1"));
        assert_eq!(result["tasks"][1]["id"], wrapped(&key, "task-2"));
        assert_eq!(result["nextPageToken"], "");
    }

    #[test]
    fn error_metadata_ids_are_wrapped_and_unrepresentable_ones_dropped() {
        let key = key();
        let ctx = context(&key);
        let mut error = json!({"code": -32001, "data": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
             "metadata": {"taskId": "task-1", "contextId": "", "other": "keep"}},
        ]});
        wrap_error_data(&mut error, &ctx);
        assert_eq!(
            error["data"][0]["metadata"]["taskId"],
            wrapped(&key, "task-1")
        );
        assert!(error["data"][0]["metadata"].get("contextId").is_none());
        assert_eq!(error["data"][0]["metadata"]["other"], "keep");
    }

    #[test]
    fn hook_field_sets_catch_changed_method_added_ids_and_dropped_events() {
        let request = json!({
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"messageId": "m", "taskId": "task-1"}},
        });
        let before = request_fields("SendMessage", &request);
        assert_eq!(
            before,
            vec![
                ("$id".into(), "1".into()),
                ("params.message.taskId".into(), "task-1".into())
            ]
        );
        // Removing an id is a change for a request.
        let mut changed = request.clone();
        changed["params"]["message"]
            .as_object_mut()
            .unwrap()
            .remove("taskId");
        assert_ne!(request_fields("SendMessage", &changed), before);
        // Adding one is too.
        let mut added = request.clone();
        added["params"]["message"]["contextId"] = json!("ctx-1");
        assert_ne!(request_fields("SendMessage", &added), before);

        let event = json!({"jsonrpc": "2.0", "id": 1, "result": {"artifactUpdate": {
            "taskId": "task-1", "contextId": "ctx-1",
            "artifact": {"artifactId": "a", "parts": []},
        }}});
        let response_before = response_fields(&event);
        let mut dropped = event.clone();
        dropped["result"]["artifactUpdate"]
            .as_object_mut()
            .unwrap()
            .remove("contextId");
        let response_after = response_fields(&dropped);
        assert!(response_after
            .iter()
            .all(|field| response_before.contains(field)));
        assert!(!response_after
            .iter()
            .any(|(path, _)| path.ends_with("contextId")));
        assert!(is_artifact_update(&event));
        assert!(!is_artifact_update(
            &json!({"result": {"statusUpdate": {}}})
        ));
    }

    #[test]
    fn snake_case_protojson_aliases_are_found_in_requests_but_not_in_metadata() {
        for params in [
            json!({"message": {"task_id": "raw"}}),
            json!({"message": {"context_id": "raw"}}),
            json!({"message": {"reference_task_ids": ["raw"]}}),
            json!({"context_id": "raw"}),
            json!({"configuration": {"task_push_notification_config": {"url": "https://x"}}}),
        ] {
            assert!(
                request_snake_alias(&json!({"params": params})).is_some(),
                "{params}"
            );
        }
        // `metadata` is a google.protobuf.Struct: those keys are not typed fields.
        assert!(request_snake_alias(&json!({
            "params": {"message": {"parts": [{"data": {"task_id": "T-42"}}]}}
        }))
        .is_none());
        assert!(request_snake_alias(&json!({
            "params": {"message": {"metadata": {"task_id": "business data"}}}
        }))
        .is_none());
        // The canonical names are what the gateway handles.
        assert!(request_snake_alias(&json!({
            "params": {"message": {"taskId": "x", "contextId": "y"}}
        }))
        .is_none());
    }

    #[test]
    fn snake_case_protojson_aliases_are_found_in_responses_and_error_metadata() {
        let task = json!({"jsonrpc": "2.0", "id": 1, "result": {"task": {
            "id": "t", "context_id": "c", "status": {"state": "TASK_STATE_WORKING"}
        }}});
        assert_eq!(
            response_snake_alias(&task).as_deref(),
            Some("result.task.context_id")
        );
        let event = json!({"jsonrpc": "2.0", "id": 1, "result": {"statusUpdate": {
            "task_id": "t", "contextId": "c", "status": {"state": "TASK_STATE_WORKING"}
        }}});
        assert!(response_snake_alias(&event).is_some());
        let error = json!({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32001, "message": "x",
            "data": [{"metadata": {"task_id": "raw"}}]
        }});
        assert_eq!(
            response_snake_alias(&error).as_deref(),
            Some("error.data[0].metadata.task_id")
        );
        // Arbitrary result metadata is left alone.
        let with_metadata = json!({"jsonrpc": "2.0", "id": 1, "result": {"task": {
            "id": "t", "metadata": {"task_id": "business data"}
        }}});
        assert!(response_snake_alias(&with_metadata).is_none());
        // Canonical ids do not trigger it.
        let canonical = json!({"jsonrpc": "2.0", "id": 1, "result": {"task": {
            "id": "t", "contextId": "c"
        }}});
        assert!(response_snake_alias(&canonical).is_none());
    }

    #[test]
    fn push_configuration_is_detected_in_every_entry_point() {
        for method in [
            "CreateTaskPushNotificationConfig",
            "GetTaskPushNotificationConfig",
            "ListTaskPushNotificationConfigs",
            "DeleteTaskPushNotificationConfig",
        ] {
            assert!(push_config_present(method, None));
        }
        let params = json!({"configuration": {"taskPushNotificationConfig": {"url": "https://x"}}});
        assert!(push_config_present("SendMessage", Some(&params)));
        assert!(push_config_present("SendStreamingMessage", Some(&params)));
        assert!(!push_config_present(
            "SendMessage",
            Some(&json!({"configuration": {}}))
        ));
        assert!(!push_config_present("SendMessage", None));
        // An explicit null still counts as carrying the key.
        let params = json!({"configuration": {"taskPushNotificationConfig": null}});
        assert!(push_config_present("SendMessage", Some(&params)));
    }
}
