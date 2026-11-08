//! A test-only router strategy that misbehaves on purpose.
//!
//! It is written against the raw component exports (the SDK's `register!` maps
//! a pick through the eligible candidate list, which would hide the host's own
//! check), so it can answer an index that is not an eligible candidate.
//!
//! | `mode` | behaviour |
//! | - | - |
//! | `pass` | answers the first eligible candidate's index |
//! | `bad_index` | answers `index` whether or not it is an eligible candidate |
//! | `stuck_configure` | never returns from `configure` (the apply-time budget must cut it) |
//!
//! `RouterInstance::pick` must reject an ineligible answer as a trap and the
//! caller must fall through (review M1); `configure` is bounded by the
//! configure budget (review M4).

use lumewarp_plugin_sdk::router::exports::lumewarp::plugin::router_strategy::{
    Candidate, Guest, GuestStrategy, RequestInfo, RouterInfo, Strategy,
};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Params {
    mode: String,
    #[serde(default)]
    index: u32,
}

struct Component;

impl Guest for Component {
    type Strategy = Misbehave;

    fn manifest() -> String {
        serde_json::json!({
            "abi": 1,
            "kind": "router-strategy",
            "name": "misbehave_test",
            "title": "Misbehave test",
            "description": "Answers an ineligible index or hangs in configure, for host tests.",
            "params_schema": {
                "type": "object",
                "additionalProperties": false,
                "properties": {
                    "mode": {"type": "string"},
                    "index": {"type": "integer", "minimum": 0}
                },
                "required": ["mode"]
            },
            "router": {
                "uses_weight": false,
                "needs": [],
                "member_schema": {"type": "object", "additionalProperties": false}
            }
        })
        .to_string()
    }
}

struct Misbehave {
    mode: String,
    index: u32,
}

impl GuestStrategy for Misbehave {
    fn configure(info: RouterInfo) -> Result<Strategy, String> {
        let params: Params = serde_json::from_str(&info.params_json).map_err(|e| e.to_string())?;
        match params.mode.as_str() {
            "pass" | "bad_index" => Ok(Strategy::new(Misbehave {
                mode: params.mode,
                index: params.index,
            })),
            "stuck_configure" => loop {
                std::hint::spin_loop();
            },
            other => Err(format!("unknown mode {other:?}")),
        }
    }

    fn pick(&self, _: RequestInfo, candidates: Vec<Candidate>, _seed: u64) -> Option<u32> {
        if self.mode == "bad_index" {
            return Some(self.index);
        }
        candidates.first().map(|candidate| candidate.index)
    }
}

lumewarp_plugin_sdk::router::export!(Component with_types_in lumewarp_plugin_sdk::router);
