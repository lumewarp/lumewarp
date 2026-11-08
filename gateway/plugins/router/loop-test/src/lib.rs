use lumewarp_plugin_sdk::{
    Candidates, ConfigError, Configure, Needs, NoParams, RequestInfo, RngCore, RouterInfo, Strategy,
};
pub struct Loop;
impl Configure<NoParams, NoParams> for Loop {
    fn configure(_: &RouterInfo<'_>, _: NoParams, _: &[NoParams]) -> Result<Self, ConfigError> {
        Ok(Self)
    }
}
impl Strategy for Loop {
    fn pick(&self, _: &RequestInfo<'_>, _: &Candidates<'_>, _: &mut dyn RngCore) -> Option<usize> {
        loop {
            std::hint::spin_loop();
        }
    }
}
lumewarp_plugin_sdk::register! {
    kind: "router-strategy", name: "loop_test", title: "Loop test", description: "Test epoch interruption.",
    uses_weight: false, needs: Needs::default(), params: NoParams, member_params: NoParams, strategy: Loop,
}
