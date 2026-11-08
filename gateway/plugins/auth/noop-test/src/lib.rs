use lumewarp_plugin_sdk::{auth_types, Authenticator, ConfigError, ConfigureAuth, NoParams};
pub struct Noop;
impl ConfigureAuth<NoParams> for Noop {
    fn configure(_: &auth_types::TargetInfo, _: NoParams) -> Result<Self, ConfigError> {
        Ok(Self)
    }
}
impl Authenticator for Noop {
    fn authorize(&self, _: auth_types::OutboundRequest) -> Result<auth_types::AuthResult, String> {
        Ok(auth_types::AuthResult {
            headers: Vec::new(),
            cache_ms: 0,
        })
    }
}
lumewarp_plugin_sdk::register! {
    kind: "upstream-auth", name: "noop_test", title: "Noop test", description: "Test authentication loading.",
    params: NoParams, authenticator: Noop,
}
