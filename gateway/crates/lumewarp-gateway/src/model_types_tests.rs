//! Cross-module model-type contract tests.
use crate::{
    auth::Caller,
    router::RoutingTable,
    runtime::Runtime,
    snapshot::{
        self,
        tests::{model, router, upstream},
    },
    transform::Transform,
    usage::{parse_response_usage, Captured, Tokens},
};
use bytes::Bytes;
use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1 as pb;

fn snapshot(kind: pb::ModelType) -> pb::Snapshot {
    let mut model = model("m", "u");
    model.r#type = kind as i32;
    pb::Snapshot {
        models: vec![model],
        upstreams: vec![upstream("u")],
        routers: vec![router("r", &["m"])],
        aliases: vec![pb::Alias {
            name: "a".into(),
            target: "r".into(),
        }],
        required_capabilities: vec![
            "model_types".into(),
            "audio_price".into(),
            "strategy:weighted".into(),
        ],
        ..Default::default()
    }
}

#[test]
fn every_name_inherits_type_and_models_list_includes_it() {
    for kind in [
        pb::ModelType::Llm,
        pb::ModelType::Embedding,
        pb::ModelType::Transcription,
        pb::ModelType::Decision,
    ] {
        let table =
            RoutingTable::compile(&snapshot(kind), AuthMode::Open, &Runtime::new()).unwrap();
        for name in ["m", "r", "a"] {
            assert_eq!(table.names[name].model_type, kind);
        }
        let list: serde_json::Value =
            serde_json::from_slice(&table.models_for(&Caller::Anonymous)).unwrap();
        for entry in list["data"].as_array().unwrap() {
            assert_eq!(entry["type"], crate::endpoint::type_name(kind));
        }
    }
}

#[test]
fn preregistered_model_metrics_only_include_its_type_endpoints() {
    let runtime = Runtime::new();
    let _table = RoutingTable::compile(
        &snapshot(pb::ModelType::Embedding),
        AuthMode::Open,
        &runtime,
    )
    .unwrap();
    let text = runtime.encode_metrics().unwrap();
    let requests: Vec<_> = text
        .lines()
        .filter(|line| line.starts_with("lumewarp_requests_total{") && line.contains("model=\"m\""))
        .collect();
    assert!(!requests.is_empty());
    assert!(requests
        .iter()
        .all(|line| line.contains("endpoint=\"embeddings\"")));
}

#[test]
fn snapshot_rejects_invalid_types_settings_and_mixed_pools() {
    let validate = |s: &pb::Snapshot| snapshot::validate(s, snapshot::SUPPORTED_CAPABILITIES);
    let mut s = snapshot(pb::ModelType::Unspecified);
    assert!(validate(&s).is_err());
    s.models[0].r#type = pb::ModelType::Embedding as i32;
    s.required_capabilities.retain(|c| c != "model_types");
    assert!(validate(&s).is_err());
    s.required_capabilities.push("model_types".into());
    assert!(validate(&s).is_ok());
    s.models[0].price = Some(pb::Price {
        output_nano_usd_per_mtok: 1,
        ..Default::default()
    });
    assert!(validate(&s).is_err());
    s.models[0].price = Some(pb::Price {
        nano_usd_per_audio_second: 1,
        ..Default::default()
    });
    assert!(validate(&s).is_err());
    s.models[0].r#type = pb::ModelType::Transcription as i32;
    assert!(validate(&s).is_ok());
    s.required_capabilities.retain(|c| c != "audio_price");
    assert!(validate(&s).is_err());
    s = snapshot(pb::ModelType::Llm);
    let mut second = model("n", "u");
    second.r#type = pb::ModelType::Embedding as i32;
    s.models.push(second);
    s.routers[0].members.push(pb::RouterMember {
        model: "n".into(),
        weight: 1,
        ..Default::default()
    });
    assert!(validate(&s).is_err());
    s.routers[0].members.pop();
    s.routers[0].fallbacks.push("n".into());
    s.required_capabilities.push("fallbacks".into());
    assert!(validate(&s).is_err());
    s.routers[0].fallbacks.clear();
    s.routers[0].traffic_split = vec![pb::TrafficSplit {
        name: "split".into(),
        weight: 1,
        members: vec![
            pb::RouterMember {
                model: "m".into(),
                weight: 1,
                ..Default::default()
            },
            pb::RouterMember {
                model: "n".into(),
                weight: 1,
                ..Default::default()
            },
        ],
    }];
    s.required_capabilities.push("traffic_split".into());
    assert!(validate(&s).is_err());
    for kind in [
        pb::ModelType::Embedding,
        pb::ModelType::Decision,
        pb::ModelType::Transcription,
    ] {
        let mut s = snapshot(kind);
        s.models[0].max_output_tokens = 10;
        s.required_capabilities.push("model_limits".into());
        assert!(validate(&s).is_err());
        s.models[0].max_output_tokens = 0;
        s.models[0].reasoning_effort = Some(pb::ReasoningEffort::default());
        s.required_capabilities.push("reasoning_effort".into());
        assert!(validate(&s).is_err());
    }
    let mut s = snapshot(pb::ModelType::Transcription);
    s.models[0].default_body_json = "{}".into();
    s.required_capabilities.push("default_body".into());
    assert!(validate(&s).is_err());
}

#[test]
fn responses_reasoning_and_output_fields_are_endpoint_specific() {
    let mut m = model("m", "u");
    m.max_output_tokens = 8;
    m.reasoning_effort = Some(pb::ReasoningEffort {
        default_level: "low".into(),
        ..Default::default()
    });
    let apply = |m: &pb::Model, body: &str, endpoint| {
        Transform::compile(m)
            .unwrap()
            .unwrap()
            .apply_endpoint(Bytes::copy_from_slice(body.as_bytes()), endpoint)
    };
    let out = apply(
        &m,
        r#"{"model":"up","reasoning":{"summary":"auto"},"max_tokens":99}"#,
        3,
    )
    .unwrap();
    let out: serde_json::Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(out["reasoning"]["effort"], "low");
    assert_eq!(out["reasoning"]["summary"], "auto");
    assert!(out.get("reasoning_effort").is_none());
    assert!(apply(&m, r#"{"model":"up","max_output_tokens":9}"#, 3).is_err());
    assert!(apply(&m, r#"{"model":"up","max_completion_tokens":99}"#, 1).is_ok());
    let raw = r#"{"model":"up","reasoning":{ "effort":"high", "budget":1.2300e+02 }}"#;
    let out = apply(&m, raw, 3).unwrap();
    assert!(std::str::from_utf8(&out)
        .unwrap()
        .contains(r#"{ "effort":"high", "budget":1.2300e+02 }"#));
    m.reasoning_effort = Some(pb::ReasoningEffort {
        mode: pb::ReasoningEffortMode::Strip as i32,
        ..Default::default()
    });
    let out = apply(&m, r#"{"model":"up","reasoning":{"effort":"high"}}"#, 3).unwrap();
    let out: serde_json::Value = serde_json::from_slice(&out).unwrap();
    assert!(out.get("reasoning").is_none());
    m.reasoning_effort = Some(pb::ReasoningEffort {
        mode: pb::ReasoningEffortMode::Mapped as i32,
        fragments_json: [(
            "high".into(),
            r#"{"reasoning":{"budget":7},"extra":{"thinking":true}}"#.into(),
        )]
        .into(),
        ..Default::default()
    });
    let out = apply(
        &m,
        r#"{"model":"up","reasoning":{"effort":"high","summary":"auto"}}"#,
        3,
    )
    .unwrap();
    let out: serde_json::Value = serde_json::from_slice(&out).unwrap();
    assert!(out["reasoning"].get("effort").is_none());
    assert_eq!(out["reasoning"]["budget"], 7);
    assert_eq!(out["reasoning"]["summary"], "auto");
    assert_eq!(out["extra"]["thinking"], true);
}

#[test]
fn embedding_defaults_do_not_inject_llm_controls() {
    let mut s = snapshot(pb::ModelType::Embedding);
    let m = &mut s.models[0];
    m.default_body_json = r#"{"encoding_format":"float"}"#.into();
    let body = Transform::compile(m)
        .unwrap()
        .unwrap()
        .apply_endpoint(
            Bytes::from_static(br#"{"model":"up","max_tokens":99999}"#),
            2,
        )
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["encoding_format"], "float");
    assert_eq!(body["max_tokens"], 99999);
    assert!(body.get("reasoning_effort").is_none());
}

#[test]
fn new_usage_shapes_and_audio_cost() {
    for body in [
        br#"{"usage":{"input_tokens":5,"output_tokens":3,"total_tokens":8}}"#.as_slice(),
        br#"{"usage":{"type":"tokens","input_tokens":5,"output_tokens":3}}"#,
    ] {
        assert_eq!(
            parse_response_usage(body),
            Captured::Reported(Tokens {
                prompt: 5,
                completion: 3,
                audio_ms: 0
            })
        );
    }
    assert_eq!(
        parse_response_usage(br#"{"usage":{"type":"duration","seconds":1.2345}}"#),
        Captured::Reported(Tokens {
            audio_ms: 1235,
            ..Default::default()
        })
    );
    let price = pb::Price {
        input_nano_usd_per_mtok: 1_000_000_000,
        output_nano_usd_per_mtok: 2_000_000_000,
        nano_usd_per_audio_second: 6_000_000,
    };
    assert_eq!(
        crate::usage::cost_nano_usd(
            &price,
            Tokens {
                prompt: 5,
                completion: 3,
                audio_ms: 1500
            }
        ),
        Some(9_011_000)
    );
    for invalid in [
        br#"{"usage":{"type":"duration","seconds":-1}}"#.as_slice(),
        br#"{"usage":{"type":"duration"}}"#,
    ] {
        assert_eq!(parse_response_usage(invalid), Captured::Invalid);
    }
}

#[test]
fn responses_and_transcription_sse_usage_are_split_invariant() {
    for (event, expected) in [
        ("event: response.completed\ndata: {\"response\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":3}}}\n\n", Tokens { prompt: 5, completion: 3, audio_ms: 0 }),
        ("event: response.incomplete\ndata: {\"response\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":3}}}\n\n", Tokens { prompt: 5, completion: 3, audio_ms: 0 }),
        ("event: response.failed\ndata: {\"response\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":3}}}\n\n", Tokens { prompt: 5, completion: 3, audio_ms: 0 }),
        ("event: transcript.text.done\ndata: {\"usage\":{\"type\":\"duration\",\"seconds\":1.5}}\n\n", Tokens { audio_ms: 1500, ..Default::default() }),
        ("data: {\"usage\":{\"type\":\"tokens\",\"input_tokens\":5,\"output_tokens\":3}}\n\n", Tokens { prompt: 5, completion: 3, audio_ms: 0 }),
    ] {
        for split in 0..=event.len() {
            let mut filter = crate::usage::SseFilter::new(false);
            let mut out = filter.push(Bytes::copy_from_slice(&event.as_bytes()[..split])).to_vec();
            out.extend_from_slice(&filter.push(Bytes::copy_from_slice(&event.as_bytes()[split..])));
            out.extend_from_slice(&filter.push_eof());
            assert_eq!(out, event.as_bytes()); assert_eq!(filter.captured(), Captured::Reported(expected));
        }
    }
}

#[test]
fn responses_nested_model_rewrite_at_every_frame_split() {
    let input = "event: response.created\r\ndata: {\"response\":{\"id\":\"keep\",\"model\":\"up\",\"other\":{\"model\":\"private\"}}}\r\n\r\nevent: response.completed\ndata: {\"response\":{\"model\":\"up\"},\"unrelated\":{\"model\":\"private\"}}\n\n";
    for split in 0..=input.len() {
        let mut rewrite =
            crate::rewrite::ModelRewriter::new(Bytes::from_static(b"\"called\""), true)
                .nested_response(true);
        let mut output = Vec::new();
        for frame in [&input.as_bytes()[..split], &input.as_bytes()[split..]] {
            rewrite.push(Bytes::copy_from_slice(frame));
            while let Some(piece) = rewrite.next_piece() {
                output.extend_from_slice(&piece);
            }
        }
        assert_eq!(
            output,
            input
                .replace("\"model\":\"up\"", "\"model\":\"called\"")
                .as_bytes(),
            "split {split}"
        );
    }
}

#[test]
fn probes_have_their_types_body() {
    for kind in [
        pb::ModelType::Llm,
        pb::ModelType::Embedding,
        pb::ModelType::Decision,
        pb::ModelType::Transcription,
    ] {
        let s = snapshot(kind);
        let m = &s.models[0];
        let body = crate::probe::body(m);
        if kind == pb::ModelType::Transcription {
            let fields =
                crate::multipart::Fields::parse(&body, crate::probe::AUDIO_CONTENT_TYPE).unwrap();
            assert_eq!(fields.model, "actual");
            assert!(!fields.stream);
            assert!(memchr::memmem::find(&body, crate::probe::wav()).is_some());
        } else {
            let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(body["model"], "actual");
            match kind {
                pb::ModelType::Llm => assert_eq!(body["messages"][0]["content"], "1 + 1 = ?"),
                pb::ModelType::Embedding => assert_eq!(body["input"], "ping"),
                pb::ModelType::Decision => assert_eq!(body["questions"][0]["type"], "predicate"),
                _ => unreachable!(),
            }
        }
    }
}
