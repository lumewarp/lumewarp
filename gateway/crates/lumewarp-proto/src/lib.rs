//! Wire types generated from `proto/` (the single source of truth): config
//! snapshot, config stream client and usage reporting client.

pub mod config {
    pub mod v1 {
        tonic::include_proto!("lumewarp.config.v1");
    }
}

pub mod usage {
    pub mod v1 {
        tonic::include_proto!("lumewarp.usage.v1");
    }
}
