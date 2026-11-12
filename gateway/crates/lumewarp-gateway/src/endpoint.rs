//! Single source of truth for proxied endpoints and their bounded labels.
use lumewarp_proto::config::v1::ModelType;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BodyKind {
    Json,
    Multipart,
}

pub struct Endpoint {
    pub path: &'static str,
    pub label: &'static str,
    pub model_type: ModelType,
    pub upstream_suffix: &'static str,
    pub body: BodyKind,
    /// One line for the OpenAPI document (`openapi.rs`). Required here so an endpoint
    /// cannot be added without appearing in the API reference.
    pub summary: &'static str,
}

impl Endpoint {
    pub fn supports_stream_options(&self) -> bool {
        matches!(self.label, "chat_completions" | "completions")
    }
}

macro_rules! endpoints {
    ($(($path:literal, $label:literal, $kind:ident, $suffix:literal, $body:ident, $summary:literal)),* $(,)?) => {
        pub const ENDPOINTS: &[Endpoint] = &[$(Endpoint { path: $path, label: $label, model_type: ModelType::$kind, upstream_suffix: $suffix, body: BodyKind::$body, summary: $summary }),*];
    };
}
endpoints! {
    ("/v1/chat/completions", "chat_completions", Llm, "/chat/completions", Json, "Chat completion"),
    ("/v1/completions", "completions", Llm, "/completions", Json, "Text completion"),
    ("/v1/embeddings", "embeddings", Embedding, "/embeddings", Json, "Embeddings"),
    ("/v1/responses", "responses", Llm, "/responses", Json, "Response (OpenAI Responses API)"),
    ("/v1/audio/transcriptions", "audio_transcriptions", Transcription, "/audio/transcriptions", Multipart, "Transcribe audio"),
    ("/v1/audio/translations", "audio_translations", Transcription, "/audio/translations", Multipart, "Translate audio to English text"),
    ("/v1/decisions", "decisions", Decision, "/decisions", Json, "Decision"),
    ("/v1/systemone", "systemone", Decision, "/systemone", Json, "System-one decision"),
}
pub const API_ENDPOINTS: [&str; ENDPOINTS.len() + 4] = {
    let mut labels = [""; ENDPOINTS.len() + 4];
    let mut i = 0;
    while i < ENDPOINTS.len() {
        labels[i] = ENDPOINTS[i].label;
        i += 1;
    }
    labels[ENDPOINTS.len()] = "models";
    labels[ENDPOINTS.len() + 1] = "healthz";
    labels[ENDPOINTS.len() + 2] = "metrics";
    labels[ENDPOINTS.len() + 3] = "other";
    labels
};
pub fn endpoint_index(path: &str) -> usize {
    ENDPOINTS
        .iter()
        .position(|e| e.path == path)
        .unwrap_or(match path {
            "/v1/models" => ENDPOINTS.len(),
            "/healthz" => ENDPOINTS.len() + 1,
            "/metrics" => ENDPOINTS.len() + 2,
            _ => ENDPOINTS.len() + 3,
        })
}
pub fn type_name(kind: ModelType) -> &'static str {
    match kind {
        ModelType::Llm => "llm",
        ModelType::Embedding => "embedding",
        ModelType::Transcription => "transcription",
        ModelType::Decision => "decision",
        ModelType::Unspecified => unreachable!("validated type"),
    }
}
