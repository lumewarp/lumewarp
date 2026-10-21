//! Usage events produced off the request path.
//!
//! W1 stub with the frozen interface: every sink drops events silently. The
//! bounded queue, batcher and `UsageService` client land in W2.
#![allow(dead_code)] // Wired into the request flow in W2.

pub use lumewarp_proto::usage::v1::UsageEvent;

pub struct UsageSink {
    _private: (),
}

impl UsageSink {
    /// Standalone mode: events are discarded.
    pub fn disabled() -> Self {
        Self { _private: () }
    }

    /// Never blocks.
    pub fn try_record(&self, event: UsageEvent) {
        drop(event);
    }
}
