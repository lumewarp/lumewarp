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
}

impl Endpoint {
    pub fn supports_stream_options(&self) -> bool {
        matches!(self.label, "chat_completions" | "completions")
    }
}

macro_rules! endpoints {
    ($(($path:literal, $label:literal, $kind:ident, $suffix:literal, $body:ident)),* $(,)?) => {
        pub const ENDPOINTS: &[Endpoint] = &[$(Endpoint { path: $path, label: $label, model_type: ModelType::$kind, upstream_suffix: $suffix, body: BodyKind::$body }),*];
    };
}
endpoints! {
    ("/v1/chat/completions", "chat_completions", Llm, "/chat/completions", Json),
    ("/v1/completions", "completions", Llm, "/completions", Json),
    ("/v1/embeddings", "embeddings", Embedding, "/embeddings", Json),
    ("/v1/responses", "responses", Llm, "/responses", Json),
    ("/v1/audio/transcriptions", "audio_transcriptions", Transcription, "/audio/transcriptions", Multipart),
    ("/v1/audio/translations", "audio_translations", Transcription, "/audio/translations", Multipart),
    ("/v1/decisions", "decisions", Decision, "/decisions", Json),
    ("/v1/systemone", "systemone", Decision, "/systemone", Json),
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
