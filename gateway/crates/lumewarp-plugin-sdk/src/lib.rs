//! Component export glue and schema helpers for gateway plugin authors.
#[doc(hidden)]
pub use lumewarp_router_api as native;
pub use lumewarp_router_api::*;
#[doc(hidden)]
pub use serde_json;
pub mod router {
    wit_bindgen::generate!({ path: "../../wit", world: "router", pub_export_macro: true });
}
pub mod auth {
    wit_bindgen::generate!({ path: "../../wit", world: "auth", pub_export_macro: true });
}
pub mod middleware {
    wit_bindgen::generate!({ path: "../../wit", world: "middleware", pub_export_macro: true });
}
use router::exports::lumewarp::plugin::router_strategy as wire;

/// Adapter retains the native read-only candidate API for existing policies.
pub struct RouterResource(pub Box<dyn Strategy>);
impl RouterResource {
    pub fn configure<S, P, M>(info: wire::RouterInfo) -> Result<Self, String>
    where
        S: Configure<P, M>,
        P: serde::de::DeserializeOwned,
        M: serde::de::DeserializeOwned,
    {
        let members: Vec<_> = info
            .members
            .iter()
            .map(|m| MemberInfo {
                model: &m.model,
                weight: m.weight,
                split: m.split.as_deref(),
            })
            .collect();
        let params = serde_json::from_str(&info.params_json).map_err(|e| format!("{e}"))?;
        let member_params = info
            .members
            .iter()
            .map(|m| serde_json::from_str(&m.params_json))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("{e}"))?;
        native::__create::<S, P, M>(
            &RouterInfo {
                name: &info.name,
                members: &members,
            },
            &params,
            &member_params,
        )
        .map(Self)
        .map_err(|e| e.to_string())
    }
    pub fn pick(
        &self,
        request: wire::RequestInfo,
        candidates: Vec<wire::Candidate>,
        seed: u64,
    ) -> Option<u32> {
        let names: Vec<_> = request.headers.iter().map(|(n, _)| n.clone()).collect();
        let lookup = |name: &str| {
            request
                .headers
                .iter()
                .find(|(n, _)| n == name)
                .map(|(_, v)| v.as_slice())
        };
        let req = RequestInfo::new(
            &request.requested,
            &request.team_id,
            &request.key_id,
            request.estimated_input_tokens,
            &names,
            &lookup,
        );
        let source = WireSource(&candidates);
        let eligible = |_: usize| true;
        let mut rng = SeedRng(fastrand::Rng::with_seed(seed));
        let picked = self.0.pick(
            &req,
            &Candidates::new(
                &source as &dyn CandidateSource,
                &eligible as &dyn Fn(usize) -> bool,
            ),
            &mut rng,
        )?;
        candidates.get(picked).map(|c| c.index)
    }
}
struct SeedRng(fastrand::Rng);
impl RngCore for SeedRng {
    fn u64(&mut self, r: std::ops::Range<u64>) -> u64 {
        self.0.u64(r)
    }
    fn u32(&mut self, r: std::ops::Range<u32>) -> u32 {
        self.0.u32(r)
    }
    fn usize(&mut self, r: std::ops::Range<usize>) -> usize {
        self.0.usize(r)
    }
}
struct WireSource<'a>(&'a [wire::Candidate]);
impl CandidateSource for WireSource<'_> {
    fn len(&self) -> usize {
        self.0.len()
    }
    fn name(&self, _: usize) -> &str {
        ""
    }
    fn member_index(&self, i: usize) -> usize {
        self.0[i].member_index as usize
    }
    fn weight(&self, i: usize) -> u64 {
        self.0[i].weight
    }
    fn in_flight(&self, i: usize) -> usize {
        self.0[i].in_flight as usize
    }
    fn latency_ewma_us(&self, i: usize) -> Option<u64> {
        self.0[i].latency_ewma_us
    }
    fn load(&self, i: usize) -> Load {
        self.0[i]
            .load
            .as_ref()
            .map(|l| Load {
                waiting: l.waiting,
                kv_usage_bp: l.kv_usage_bp,
                fresh: l.fresh,
            })
            .unwrap_or(Load {
                waiting: 0,
                kv_usage_bp: 0,
                fresh: false,
            })
    }
    fn total_weight(&self) -> u64 {
        self.0.iter().map(|c| c.weight).sum()
    }
    fn weighted_index(&self, mut draw: u64) -> usize {
        for (i, c) in self.0.iter().enumerate() {
            if draw < c.weight {
                return i;
            }
            draw -= c.weight;
        }
        0
    }
}

pub use auth::exports::lumewarp::plugin::upstream_auth as auth_types;
pub use middleware::exports::lumewarp::plugin::a2a_middleware as middleware_types;

pub trait Authenticator: Send + Sync + 'static {
    fn authorize(
        &self,
        request: auth_types::OutboundRequest,
    ) -> Result<auth_types::AuthResult, String>;
}
pub trait ConfigureAuth<P>: Authenticator + Sized {
    fn configure(target: &auth_types::TargetInfo, params: P) -> Result<Self, ConfigError>;
}
pub trait Middleware: Send + Sync + 'static {
    fn on_request(
        &self,
        method: String,
        body: Vec<u8>,
    ) -> Result<Vec<u8>, middleware_types::RpcError>;
    fn on_response(
        &self,
        method: String,
        body: Vec<u8>,
    ) -> Result<Vec<u8>, middleware_types::RpcError>;
    fn on_stream_event(
        &self,
        method: String,
        data: Vec<u8>,
    ) -> Result<Option<Vec<u8>>, middleware_types::RpcError>;
}
pub trait ConfigureMiddleware<P>: Middleware + Sized {
    fn configure(agent: &middleware_types::AgentInfo, params: P) -> Result<Self, ConfigError>;
}
#[doc(hidden)]
pub fn manifest<P: schemars::JsonSchema>(
    kind: &str,
    name: &str,
    title: &str,
    description: &str,
) -> String {
    serde_json::json!({"abi": 1, "kind": kind, "name": name, "title": title, "description": description, "params_schema": schema_for::<P>()}).to_string()
}

#[macro_export]
macro_rules! register {
    (kind: "router-strategy", name: $name:literal, title: $title:literal, description: $description:literal,
     params: $params:ty, member_params: $member:ty, strategy: $strategy:ty $(,)?) => {
        $crate::register! { kind: "router-strategy", name: $name, title: $title, description: $description,
            uses_weight: false, needs: $crate::Needs::default(), params: $params, member_params: $member, strategy: $strategy }
    };
    (kind: "upstream-auth", name: $name:literal, title: $title:literal, description: $description:literal,
     params: $params:ty, authenticator: $auth:ty $(,)?) => {
        struct Component;
        impl $crate::auth_types::Guest for Component {
            type Authenticator = GuestAuth;
            fn manifest() -> String { $crate::manifest::<$params>("upstream-auth", $name, $title, $description) }
        }
        struct GuestAuth($auth);
        impl $crate::auth_types::GuestAuthenticator for GuestAuth {
            fn configure(target: $crate::auth_types::TargetInfo) -> Result<$crate::auth_types::Authenticator, String> {
                let params: $params = $crate::serde_json::from_str(&target.params_json).map_err(|e| e.to_string())?;
                <$auth as $crate::ConfigureAuth<$params>>::configure(&target, params)
                    .map(|a| $crate::auth_types::Authenticator::new(Self(a))).map_err(|e| e.to_string())
            }
            fn authorize(&self, request: $crate::auth_types::OutboundRequest) -> Result<$crate::auth_types::AuthResult, String> {
                <$auth as $crate::Authenticator>::authorize(&self.0, request)
            }
        }
        #[cfg(target_arch = "wasm32")]
        $crate::auth::export!(Component with_types_in $crate::auth);
        #[cfg(test)] #[test] fn plugin_schema_subset() { $crate::assert_schema_subset::<$params>(); }
    };
    (kind: "a2a-middleware", name: $name:literal, title: $title:literal, description: $description:literal,
     params: $params:ty, middleware: $middleware:ty $(,)?) => {
        struct Component;
        impl $crate::middleware_types::Guest for Component {
            type Middleware = GuestMiddleware;
            fn manifest() -> String { $crate::manifest::<$params>("a2a-middleware", $name, $title, $description) }
        }
        struct GuestMiddleware($middleware);
        impl $crate::middleware_types::GuestMiddleware for GuestMiddleware {
            fn configure(agent: $crate::middleware_types::AgentInfo) -> Result<$crate::middleware_types::Middleware, String> {
                let params: $params = $crate::serde_json::from_str(&agent.params_json).map_err(|e| e.to_string())?;
                <$middleware as $crate::ConfigureMiddleware<$params>>::configure(&agent, params)
                    .map(|m| $crate::middleware_types::Middleware::new(Self(m))).map_err(|e| e.to_string())
            }
            fn on_request(&self, method: String, body: Vec<u8>) -> Result<Vec<u8>, $crate::middleware_types::RpcError> {
                <$middleware as $crate::Middleware>::on_request(&self.0, method, body)
            }
            fn on_response(&self, method: String, body: Vec<u8>) -> Result<Vec<u8>, $crate::middleware_types::RpcError> {
                <$middleware as $crate::Middleware>::on_response(&self.0, method, body)
            }
            fn on_stream_event(&self, method: String, data: Vec<u8>) -> Result<Option<Vec<u8>>, $crate::middleware_types::RpcError> {
                <$middleware as $crate::Middleware>::on_stream_event(&self.0, method, data)
            }
        }
        #[cfg(target_arch = "wasm32")]
        $crate::middleware::export!(Component with_types_in $crate::middleware);
        #[cfg(test)] #[test] fn plugin_schema_subset() { $crate::assert_schema_subset::<$params>(); }
    };
    (kind: "router-strategy", name: $name:literal, title: $title:literal, description: $description:literal,
     uses_weight: $weight:expr, needs: $needs:expr, params: $params:ty, member_params: $member:ty, strategy: $strategy:ty $(,)?) => {
        // Native registration is only for the plugin crate's policy and schema tests;
        // the gateway never links this crate, it loads its component exports.
        #[cfg(not(target_arch = "wasm32"))]
        $crate::native::register! { name: $name, title: $title, description: $description, uses_weight: $weight, needs: $needs, params: $params, member_params: $member, strategy: $strategy }
        struct Component;
        impl $crate::router::exports::lumewarp::plugin::router_strategy::Guest for Component {
            type Strategy = GuestStrategy;
            fn manifest() -> String {
                let needs: $crate::Needs = $needs;
                let mut signals = Vec::<&str>::new();
                if needs.latency { signals.push("latency"); }
                if needs.vllm_load { signals.push("vllm_load"); }
                $crate::serde_json::json!({ "abi": 1, "kind": "router-strategy", "name": $name, "title": $title, "description": $description,
                    "params_schema": $crate::schema_for::<$params>(), "router": { "uses_weight": $weight, "needs": signals, "member_schema": $crate::schema_for::<$member>() } }).to_string()
            }
        }
        struct GuestStrategy($crate::RouterResource);
        impl $crate::router::exports::lumewarp::plugin::router_strategy::GuestStrategy for GuestStrategy {
            fn configure(info: $crate::router::exports::lumewarp::plugin::router_strategy::RouterInfo) -> Result<$crate::router::exports::lumewarp::plugin::router_strategy::Strategy, String> {
                $crate::RouterResource::configure::<$strategy, $params, $member>(info).map(|r| $crate::router::exports::lumewarp::plugin::router_strategy::Strategy::new(Self(r)))
            }
            fn pick(&self, request: $crate::router::exports::lumewarp::plugin::router_strategy::RequestInfo, candidates: Vec<$crate::router::exports::lumewarp::plugin::router_strategy::Candidate>, seed: u64) -> Option<u32> {
                self.0.pick(request, candidates, seed)
            }
        }
        #[cfg(target_arch = "wasm32")]
        $crate::router::export!(Component with_types_in $crate::router);
    };
}
