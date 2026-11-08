use lumewarp_plugin_sdk::{
    middleware_types, ConfigError, ConfigureMiddleware, Middleware, NoParams,
};
pub struct Noop;
impl ConfigureMiddleware<NoParams> for Noop {
    fn configure(_: &middleware_types::AgentInfo, _: NoParams) -> Result<Self, ConfigError> {
        Ok(Self)
    }
}
impl Middleware for Noop {
    fn on_request(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        Ok(body)
    }
    fn on_response(&self, _: String, body: Vec<u8>) -> Result<Vec<u8>, middleware_types::RpcError> {
        Ok(body)
    }
    fn on_stream_event(
        &self,
        _: String,
        data: Vec<u8>,
    ) -> Result<Option<Vec<u8>>, middleware_types::RpcError> {
        Ok(Some(data))
    }
}
lumewarp_plugin_sdk::register! {
    kind: "a2a-middleware", name: "noop_test", title: "Noop test", description: "Test middleware loading.",
    params: NoParams, middleware: Noop,
}
