//! Type-specific synthetic probes; audio is generated once, not stored as an asset.
use bytes::Bytes;
use lumewarp_proto::config::v1::{Model, ModelType};
use std::sync::OnceLock;

pub const AUDIO_CONTENT_TYPE: &str = "multipart/form-data; boundary=lumewarp-probe";
pub fn endpoint(kind: i32) -> usize {
    crate::endpoint::ENDPOINTS
        .iter()
        .position(|e| e.model_type as i32 == kind)
        .expect("validated type")
}
pub fn wav() -> &'static [u8] {
    static WAV: OnceLock<Vec<u8>> = OnceLock::new();
    WAV.get_or_init(|| {
        let mut bytes = Vec::with_capacity(32044);
        bytes.extend_from_slice(b"RIFF");
        bytes.extend_from_slice(&32036u32.to_le_bytes());
        bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16u32.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&16000u32.to_le_bytes());
        bytes.extend_from_slice(&32000u32.to_le_bytes());
        bytes.extend_from_slice(&2u16.to_le_bytes());
        bytes.extend_from_slice(&16u16.to_le_bytes());
        bytes.extend_from_slice(b"data");
        bytes.extend_from_slice(&32000u32.to_le_bytes());
        bytes.resize(32044, 0);
        bytes
    })
}
pub fn body(model: &Model) -> Bytes {
    if model.r#type == ModelType::Transcription as i32 {
        let mut bytes = format!("--lumewarp-probe\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{}\r\n--lumewarp-probe\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n--lumewarp-probe\r\nContent-Disposition: form-data; name=\"file\"; filename=\"probe.wav\"\r\nContent-Type: audio/wav\r\n\r\n", model.upstream_model).into_bytes();
        bytes.extend_from_slice(wav());
        bytes.extend_from_slice(b"\r\n--lumewarp-probe--\r\n");
        return Bytes::from(bytes);
    }
    let value = match ModelType::try_from(model.r#type).expect("validated type") {
        ModelType::Embedding => serde_json::json!({"model":model.upstream_model,"input":"ping"}),
        ModelType::Decision => {
            serde_json::json!({"model":model.upstream_model,"input":"ping","questions":[{"type":"predicate","name":"ok","instructions":"Is the input a word?"}]})
        }
        _ => {
            serde_json::json!({"model":model.upstream_model,"messages":[{"role":"user","content":"1 + 1 = ?"}],"max_tokens":if model.max_output_tokens == 0 {16} else {model.max_output_tokens.min(16)},"stream":false})
        }
    };
    Bytes::from(serde_json::to_vec(&value).expect("JSON"))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn generated_wav_header() {
        assert_eq!(wav().len(), 32044);
        assert_eq!(&wav()[..44], b"RIFF\x24\x7d\0\0WAVEfmt \x10\0\0\0\x01\0\x01\0\x80\x3e\0\0\0\x7d\0\0\x02\0\x10\0data\0\x7d\0\0");
        assert!(wav()[44..].iter().all(|&b| b == 0));
    }
}
