//! `my_strategy`: use the member with the lowest priority number, and among
//! equal priorities the one with the fewest requests in flight.
//!
//! Replace the policy, keep the shape: settings are parsed and checked in
//! `configure`, `pick` only reads the precomputed table and the candidates.

use lumewarp_plugin_sdk::{
    Candidates, ConfigError, Configure, Needs, RequestInfo, RngCore, RouterInfo, Strategy,
};
use schemars::JsonSchema;
use serde::Deserialize;

/// Router settings (`Router.params_json`).
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Skip members with more requests in flight than this.
    #[schemars(title = "Max in flight", range(min = 1))]
    pub max_in_flight: Option<u32>,
}

/// Settings of one router member (`RouterMember.params_json`).
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct MemberParams {
    /// Lower numbers are used first.
    #[schemars(title = "Priority", range(min = 1))]
    pub priority: u32,
}

pub struct MyStrategy {
    max_in_flight: usize,
    /// By `Candidate::member_index()`, not by pool position.
    priorities: Box<[u32]>,
}

impl Configure<Params, MemberParams> for MyStrategy {
    fn configure(
        _: &RouterInfo<'_>,
        params: Params,
        members: &[MemberParams],
    ) -> Result<Self, ConfigError> {
        // Schema annotations are not checked at runtime: standalone YAML and
        // snapshots reach `configure` without the schema. Enforce every bound.
        if params.max_in_flight == Some(0) {
            return Err(ConfigError("max_in_flight must be at least 1".into()));
        }
        if members.iter().any(|m| m.priority == 0) {
            return Err(ConfigError("priority must be at least 1".into()));
        }
        Ok(Self {
            max_in_flight: params.max_in_flight.map_or(usize::MAX, |max| max as usize),
            priorities: members.iter().map(|m| m.priority).collect(),
        })
    }
}

impl Strategy for MyStrategy {
    fn pick(
        &self,
        _: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        _: &mut dyn RngCore,
    ) -> Option<usize> {
        candidates
            .iter()
            .filter(|c| c.in_flight() <= self.max_in_flight)
            .min_by_key(|c| (self.priorities[c.member_index()], c.in_flight()))
            // Return the pool position, never the member index.
            .map(|c| c.index)
    }
}

lumewarp_plugin_sdk::register! {
    kind: "router-strategy",
    name: "my_strategy",
    title: "My strategy",
    description: "Use the lowest priority number, then the fewest requests in flight.",
    uses_weight: false,
    needs: Needs::default(),
    params: Params,
    member_params: MemberParams,
    strategy: MyStrategy,
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumewarp_plugin_sdk::{lookup, CandidateSource, Load, MemberInfo, ThreadRng};
    use serde_json::{json, Value};

    /// A pool of candidates: (model name, requests in flight). Pool position
    /// and member index are the same here (one pool, no traffic split).
    struct Pool(&'static [(&'static str, usize)]);

    impl CandidateSource for Pool {
        fn len(&self) -> usize {
            self.0.len()
        }
        fn name(&self, i: usize) -> &str {
            self.0[i].0
        }
        fn member_index(&self, i: usize) -> usize {
            i
        }
        fn weight(&self, _: usize) -> u64 {
            1
        }
        fn in_flight(&self, i: usize) -> usize {
            self.0[i].1
        }
        fn latency_ewma_us(&self, _: usize) -> Option<u64> {
            None
        }
        fn load(&self, _: usize) -> Load {
            Load {
                waiting: 0,
                kv_usage_bp: 0,
                fresh: false,
            }
        }
        fn total_weight(&self) -> u64 {
            self.0.len() as u64
        }
        fn weighted_index(&self, draw: u64) -> usize {
            draw as usize
        }
    }

    const MEMBERS: [MemberInfo<'static>; 3] = [
        MemberInfo {
            model: "a",
            weight: 1,
            split: None,
        },
        MemberInfo {
            model: "b",
            weight: 1,
            split: None,
        },
        MemberInfo {
            model: "c",
            weight: 1,
            split: None,
        },
    ];

    /// Configures the strategy through the same path the gateway uses.
    fn create(
        params: Value,
        members: &[Value],
    ) -> Result<Box<dyn lumewarp_plugin_sdk::Strategy>, String> {
        let factory = lookup("my_strategy").unwrap().unwrap();
        let info = RouterInfo {
            name: "r",
            members: &MEMBERS[..members.len()],
        };
        (factory.create)(&info, &params, members).map_err(|e| e.to_string())
    }

    fn pick(strategy: &dyn lumewarp_plugin_sdk::Strategy, pool: &Pool) -> Option<usize> {
        let eligible = |_: usize| true;
        let request = RequestInfo::new("r", "", "", None, &[], &|_| None);
        strategy.pick(
            &request,
            &Candidates::new(
                pool as &dyn CandidateSource,
                &eligible as &dyn Fn(usize) -> bool,
            ),
            &mut ThreadRng,
        )
    }

    #[test]
    fn the_lowest_priority_wins_then_the_least_loaded() {
        let members = [
            json!({"priority": 2}),
            json!({"priority": 1}),
            json!({"priority": 1}),
        ];
        let strategy = create(json!({}), &members).unwrap();
        assert_eq!(
            pick(&*strategy, &Pool(&[("a", 0), ("b", 5), ("c", 3)])),
            Some(2)
        );
        assert_eq!(
            pick(&*strategy, &Pool(&[("a", 0), ("b", 1), ("c", 3)])),
            Some(1)
        );
    }

    #[test]
    fn nothing_acceptable_is_none() {
        let members = [json!({"priority": 1})];
        let strategy = create(json!({"max_in_flight": 2}), &members).unwrap();
        assert_eq!(pick(&*strategy, &Pool(&[("a", 3)])), None);
    }

    #[test]
    fn configure_enforces_what_the_schema_states() {
        assert!(create(json!({"max_in_flight": 0}), &[json!({"priority": 1})]).is_err());
        assert!(create(json!({}), &[json!({"priority": 0})]).is_err());
        assert!(create(json!({"unknown": 1}), &[json!({"priority": 1})]).is_err());
        assert!(create(json!({}), &[json!({})]).is_err());
    }
}
