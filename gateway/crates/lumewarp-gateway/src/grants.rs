//! Who may see and call what on the MCP and A2A endpoints.
//!
//! The control plane already intersects grants along org -> team -> owner -> key
//! (`EffectivePolicy.mcp_grants` / `allowed_agents`), so this module only has to
//! apply the result: one hash lookup per question, precomputed when the snapshot
//! is applied and stored on the key's compiled context.
//!
//! Rules (`snapshot.proto`, `McpGrant`):
//! - `allow_all_mcp` wins over any grant list; an empty list with
//!   `allow_all_mcp = false` grants nothing.
//! - A server absent from the list is invisible: not listed, not callable.
//! - A granted server exposes its approved tools filtered by `all_tools` /
//!   `tools`, and **all** of its prompts (`granted server ⇒ all prompts`).
//! - Tool grants are by upstream name, without the `<server>__` prefix.

// The MCP/A2A request path (W2) wires these modules; until then most of the
// API has no caller in the binary, which `-D warnings` would reject.
#![allow(dead_code)]
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use lumewarp_proto::config::v1::EffectivePolicy;

/// One server's grant: every approved tool, or a named subset.
#[derive(Clone, Debug, Default)]
struct ServerGrant {
    all_tools: bool,
    tools: HashSet<Arc<str>>,
}

/// What one key may see on MCP servers.
#[derive(Clone, Debug, Default)]
pub struct McpGrants {
    allow_all: bool,
    servers: HashMap<Arc<str>, ServerGrant>,
}

impl McpGrants {
    /// Applies `policy.allow_all_mcp` and `policy.mcp_grants`.
    pub fn compile(policy: &EffectivePolicy) -> Self {
        let mut grants = Self {
            allow_all: policy.allow_all_mcp,
            servers: HashMap::new(),
        };
        for grant in &policy.mcp_grants {
            if grant.server.is_empty() {
                continue;
            }
            let entry = grants
                .servers
                .entry(grant.server.as_str().into())
                .or_default();
            entry.all_tools |= grant.all_tools;
            for tool in &grant.tools {
                if !tool.is_empty() {
                    entry.tools.insert(tool.as_str().into());
                }
            }
        }
        grants
    }

    /// Whether the key may see or call this server at all.
    pub fn may_see_server(&self, server: &str) -> bool {
        self.allow_all || self.servers.contains_key(server)
    }

    /// Whether the key may see the tool: the server is granted and the tool is
    /// either covered by `all_tools` or named. Approval by hash is separate
    /// (`targets::Approved`) and applies on top.
    pub fn may_see_tool(&self, server: &str, tool: &str) -> bool {
        if self.allow_all {
            return true;
        }
        let Some(grant) = self.servers.get(server) else {
            return false;
        };
        grant.all_tools || grant.tools.contains(tool)
    }

    /// Whether the key may call the tool at all. Same answer as
    /// [`Self::may_see_tool`]: an invisible tool cannot be called either.
    pub fn may_call_tool(&self, server: &str, tool: &str) -> bool {
        self.may_see_tool(server, tool)
    }

    /// Prompts of a granted server are always allowed.
    pub fn may_see_prompt(&self, server: &str, prompt: &str) -> bool {
        let _ = prompt;
        self.may_see_server(server)
    }

    /// Whether every tool of a granted server is allowed, which lets the
    /// aggregated endpoint skip the per-tool grant check.
    pub fn all_tools(&self, server: &str) -> bool {
        self.allow_all
            || self
                .servers
                .get(server)
                .is_some_and(|grant| grant.all_tools)
    }

    /// The servers this key may see, in no particular order. Combined with
    /// `allow_all_mcp`, the caller iterates its configured servers instead.
    pub fn servers(&self) -> impl Iterator<Item = &str> {
        self.servers.keys().map(|name| name.as_ref())
    }

    /// True when the key may see every configured server.
    pub fn allows_all(&self) -> bool {
        self.allow_all
    }

    pub fn is_empty(&self) -> bool {
        !self.allow_all && self.servers.is_empty()
    }
}

/// What one key may call on A2A agents.
#[derive(Clone, Debug, Default)]
pub struct AgentGrants {
    allow_all: bool,
    agents: HashSet<Arc<str>>,
}

impl AgentGrants {
    /// Applies `policy.allow_all_agents` and `policy.allowed_agents`.
    pub fn compile(policy: &EffectivePolicy) -> Self {
        Self {
            allow_all: policy.allow_all_agents,
            agents: policy
                .allowed_agents
                .iter()
                .filter(|name| !name.is_empty())
                .map(|name| name.as_str().into())
                .collect(),
        }
    }

    pub fn may_call(&self, agent: &str) -> bool {
        self.allow_all || self.agents.contains(agent)
    }

    pub fn agents(&self) -> impl Iterator<Item = &str> {
        self.agents.iter().map(|name| name.as_ref())
    }

    pub fn allows_all(&self) -> bool {
        self.allow_all
    }

    pub fn is_empty(&self) -> bool {
        !self.allow_all && self.agents.is_empty()
    }
}

/// Everything a key may reach outside the LLM endpoints.
#[derive(Clone, Debug, Default)]
pub struct Grants {
    pub mcp: McpGrants,
    pub agents: AgentGrants,
}

impl Grants {
    /// Precomputes the per-key grants from the compiled policy.
    pub fn compile(policy: &EffectivePolicy) -> Self {
        Self {
            mcp: McpGrants::compile(policy),
            agents: AgentGrants::compile(policy),
        }
    }

    /// `EffectivePolicy` lives in the snapshot; this is what a key compiled
    /// without MCP/A2A policy gets: nothing.
    pub fn none() -> Self {
        Self::default()
    }

    pub fn may_see_server(&self, server: &str) -> bool {
        self.mcp.may_see_server(server)
    }

    pub fn may_call_tool(&self, server: &str, tool: &str) -> bool {
        self.mcp.may_call_tool(server, tool)
    }

    pub fn may_see_prompt(&self, server: &str, prompt: &str) -> bool {
        self.mcp.may_see_prompt(server, prompt)
    }

    pub fn may_call_agent(&self, agent: &str) -> bool {
        self.agents.may_call(agent)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumewarp_proto::config::v1::McpGrant;

    fn grant(server: &str, all_tools: bool, tools: &[&str]) -> McpGrant {
        McpGrant {
            server: server.into(),
            all_tools,
            tools: tools.iter().map(|tool| (*tool).to_owned()).collect(),
        }
    }

    fn policy(
        allow_all_mcp: bool,
        grants: Vec<McpGrant>,
        allow_all_agents: bool,
        agents: &[&str],
    ) -> EffectivePolicy {
        EffectivePolicy {
            allow_all_mcp,
            mcp_grants: grants,
            allow_all_agents,
            allowed_agents: agents.iter().map(|agent| (*agent).to_owned()).collect(),
            ..EffectivePolicy::default()
        }
    }

    #[test]
    fn a_server_grant_exposes_only_its_tools_and_every_prompt() {
        let grants = Grants::compile(&policy(
            false,
            vec![grant("s1", false, &["echo", "add"])],
            false,
            &[],
        ));
        assert!(grants.may_see_server("s1"));
        assert!(grants.may_call_tool("s1", "echo"));
        assert!(grants.may_call_tool("s1", "add"));
        assert!(!grants.may_call_tool("s1", "slow"));
        assert!(grants.may_see_prompt("s1", "greet"));
        assert!(grants.may_see_prompt("s1", "anything"));
        // A server that was not granted is invisible, tools and prompts alike.
        assert!(!grants.may_see_server("s2"));
        assert!(!grants.may_call_tool("s2", "echo"));
        assert!(!grants.may_see_prompt("s2", "greet"));
        assert!(!grants.mcp.all_tools("s1"));
        assert!(!grants.mcp.is_empty());
        assert!(grants.agents.is_empty());
    }

    #[test]
    fn all_tools_grants_cover_tools_the_gateway_has_never_seen() {
        let grants = Grants::compile(&policy(false, vec![grant("s1", true, &[])], false, &[]));
        assert!(grants.mcp.all_tools("s1"));
        assert!(grants.may_call_tool("s1", "brand-new-tool"));
        assert!(!grants.mcp.all_tools("s2"));
    }

    #[test]
    fn a_granted_server_with_no_tools_still_serves_prompts() {
        let grants = Grants::compile(&policy(false, vec![grant("s1", false, &[])], false, &[]));
        assert!(grants.may_see_server("s1"));
        assert!(!grants.may_call_tool("s1", "echo"));
        assert!(grants.may_see_prompt("s1", "greet"));
        assert!(!grants.mcp.all_tools("s1"));
    }

    #[test]
    fn allow_all_wins_over_the_list_and_an_empty_list_grants_nothing() {
        let all = Grants::compile(&policy(true, vec![], false, &[]));
        assert!(all.may_see_server("anything"));
        assert!(all.may_call_tool("anything", "tool"));
        assert!(all.may_see_prompt("anything", "prompt"));
        assert!(all.mcp.all_tools("anything"));
        assert!(all.mcp.allows_all());
        assert!(!all.mcp.is_empty());

        let nothing = Grants::compile(&policy(false, vec![], false, &[]));
        assert!(!nothing.may_see_server("s1"));
        assert!(!nothing.may_call_tool("s1", "echo"));
        assert!(!nothing.may_see_prompt("s1", "greet"));
        assert!(nothing.mcp.is_empty());
        assert!(Grants::none().mcp.is_empty());
        assert!(!Grants::none().may_call_agent("a1"));
    }

    #[test]
    fn duplicate_grants_merge() {
        let grants = Grants::compile(&policy(
            false,
            vec![
                grant("s1", false, &["echo"]),
                grant("s1", false, &["add", "echo"]),
            ],
            false,
            &[],
        ));
        assert!(grants.may_call_tool("s1", "echo"));
        assert!(grants.may_call_tool("s1", "add"));
        let merged = Grants::compile(&policy(
            false,
            vec![grant("s1", true, &[]), grant("s1", false, &["echo"])],
            false,
            &[],
        ));
        assert!(merged.mcp.all_tools("s1"));
        assert!(merged.may_call_tool("s1", "anything"));
    }

    #[test]
    fn empty_entries_grant_nothing() {
        let grants = Grants::compile(&policy(
            false,
            vec![grant("", true, &["echo"]), grant("s1", false, &[""])],
            false,
            &["", "a1"],
        ));
        assert!(!grants.may_see_server(""));
        assert!(grants.may_see_server("s1"));
        assert!(!grants.may_call_tool("s1", ""));
        assert!(!grants.may_call_tool("s1", "echo"));
        assert!(grants.may_call_agent("a1"));
        assert!(!grants.may_call_agent(""));
        assert_eq!(grants.mcp.servers().count(), 1);
        assert_eq!(grants.agents.agents().count(), 1);
    }

    #[test]
    fn agents_are_allowed_by_name_or_allow_all() {
        let grants = Grants::compile(&policy(false, vec![], false, &["a1", "a2"]));
        assert!(grants.may_call_agent("a1"));
        assert!(grants.may_call_agent("a2"));
        assert!(!grants.may_call_agent("a3"));
        assert!(!grants.agents.allows_all());
        let all = Grants::compile(&policy(false, vec![], true, &[]));
        assert!(all.may_call_agent("anything"));
        assert!(all.agents.allows_all());
        assert!(!all.agents.is_empty());
    }

    #[test]
    fn server_names_are_exact() {
        let grants = Grants::compile(&policy(false, vec![grant("s1", true, &[])], false, &[]));
        assert!(grants.may_see_server("s1"));
        assert!(!grants.may_see_server("s1 "));
        assert!(!grants.may_see_server("S1"));
        assert!(!grants.may_see_server("s1__echo"));
    }
}
