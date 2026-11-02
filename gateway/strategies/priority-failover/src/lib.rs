//! Picks the eligible member with the lowest `priority`. This crate is all a
//! strategy needs: nothing in the proto, the control plane or the UI names it.
//! Built into a gateway with `--features strategy-priority-failover`.

use lumewarp_router_api::{
    Candidates, ConfigError, Configure, Needs, RequestInfo, RngCore, RouterInfo, Strategy,
};
use schemars::JsonSchema;
use serde::Deserialize;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Skip members with more requests in flight than this.
    #[schemars(title = "Max in flight", range(min = 1))]
    pub max_in_flight: Option<u32>,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct MemberParams {
    /// Lower numbers are used first.
    #[schemars(title = "Priority", range(min = 1))]
    pub priority: u32,
}

pub struct PriorityFailover {
    max_in_flight: usize,
    /// By `Candidate::member_index`: stable across traffic-split pools.
    priorities: Box<[u32]>,
}

impl Configure<Params, MemberParams> for PriorityFailover {
    fn configure(
        _: &RouterInfo<'_>,
        params: Params,
        members: &[MemberParams],
    ) -> Result<Self, ConfigError> {
        Ok(Self {
            max_in_flight: params.max_in_flight.map_or(usize::MAX, |max| max as usize),
            priorities: members.iter().map(|m| m.priority).collect(),
        })
    }
}

impl Strategy for PriorityFailover {
    fn pick(
        &self,
        _: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        _: &mut dyn RngCore,
    ) -> Option<usize> {
        candidates
            .iter()
            .filter(|c| c.in_flight() <= self.max_in_flight)
            .min_by_key(|c| self.priorities[c.member_index()])
            .map(|c| c.index)
    }
}

lumewarp_router_api::register! {
    name: "priority_failover",
    title: "Priority failover",
    description: "Use the lowest priority number that is healthy.",
    uses_weight: false,
    needs: Needs::default(),
    params: Params,
    member_params: MemberParams,
    strategy: PriorityFailover,
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumewarp_router_api::{lookup, CandidateSource, Load, MemberInfo, ThreadRng};
    use serde_json::json;

    /// One traffic-split pool of a router: entries `first..first + names.len()`.
    struct Pool {
        first: usize,
        names: &'static [&'static str],
    }

    impl CandidateSource for Pool {
        fn len(&self) -> usize {
            self.names.len()
        }
        fn name(&self, i: usize) -> &str {
            self.names[i]
        }
        fn member_index(&self, i: usize) -> usize {
            self.first + i
        }
        fn weight(&self, _: usize) -> u64 {
            1
        }
        fn in_flight(&self, _: usize) -> usize {
            0
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
            self.names.len() as u64
        }
        fn weighted_index(&self, draw: u64) -> usize {
            draw as usize
        }
    }

    const MEMBERS: [MemberInfo<'static>; 3] = [
        MemberInfo {
            model: "a",
            weight: 1,
            split: Some("x"),
        },
        MemberInfo {
            model: "b",
            weight: 1,
            split: Some("x"),
        },
        MemberInfo {
            model: "a",
            weight: 1,
            split: Some("y"),
        },
    ];

    fn create(params: serde_json::Value, members: &[serde_json::Value]) -> Box<dyn Strategy> {
        let factory = lookup("priority_failover").unwrap().unwrap();
        let info = RouterInfo {
            name: "r",
            members: &MEMBERS,
        };
        (factory.create)(&info, &params, members).unwrap_or_else(|e| panic!("{e}"))
    }

    #[test]
    fn the_same_model_has_its_own_priority_in_each_split() {
        let members = [
            json!({"priority": 2}),
            json!({"priority": 1}),
            json!({"priority": 9}),
        ];
        let strategy = create(json!({}), &members);
        let pick = |pool: &Pool| {
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
        };
        let first = Pool {
            first: 0,
            names: &["a", "b"],
        };
        assert_eq!(pick(&first), Some(1), "b has priority 1 in split x");
        // Same pool position, other member: priority 9 instead of 2 for "a".
        let second = Pool {
            first: 1,
            names: &["a", "b"],
        };
        assert_eq!(
            pick(&second),
            Some(0),
            "member 1 (priority 1) beats member 2"
        );
    }

    #[test]
    fn configure_is_strict_about_the_declared_settings() {
        let factory = lookup("priority_failover").unwrap().unwrap();
        let info = RouterInfo {
            name: "r",
            members: &MEMBERS[..1],
        };
        let create = |params, members: &[serde_json::Value]| {
            (factory.create)(&info, &params, members).map(|_| ())
        };
        assert!(create(json!({}), &[json!({"priority": 1})]).is_ok());
        assert!(create(json!({"unknown": 1}), &[json!({"priority": 1})]).is_err());
        assert!(
            create(json!({}), &[json!({})]).is_err(),
            "priority is required"
        );
        assert!(create(json!({}), &[json!({"priority": "1"})]).is_err());
    }

    #[test]
    fn the_descriptor_comes_from_the_structs() {
        let descriptor = lumewarp_router_api::descriptors()
            .into_iter()
            .find(|d| d.name == "priority_failover")
            .unwrap();
        assert!(!descriptor.uses_weight);
        assert!(descriptor.needs.is_empty());
        assert_eq!(
            descriptor.member_schema["properties"]["priority"]["title"],
            json!("Priority")
        );
        assert_eq!(descriptor.member_schema["required"], json!(["priority"]));
        assert_eq!(descriptor.digest.len(), 64);
    }
}
