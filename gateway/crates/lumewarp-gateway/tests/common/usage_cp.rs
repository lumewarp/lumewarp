//! In-process fake control plane with both services: ConfigService sends one full
//! snapshot and UsageService collects the events (hand-written tonic services,
//! since the proto crate only generates clients).

#![allow(dead_code)]

use std::{
    convert::Infallible,
    net::SocketAddr,
    pin::Pin,
    sync::{
        atomic::{AtomicU32, AtomicU64, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::Duration,
};

use lumewarp_proto::{
    config::v1::{config_update::Update, ConfigUpdate, GatewayMessage, Snapshot},
    usage::v1::{ReportRequest, ReportResponse, UsageEvent},
};
use tokio::{net::TcpListener, sync::mpsc, task::JoinHandle};
use tokio_stream::{wrappers::TcpListenerStream, Stream};
use tonic::{
    codegen::{http, BoxFuture, Service},
    server::NamedService,
    Status,
};

pub struct CpState {
    /// `None`: the stream stays open and never sends a snapshot.
    snapshot: Option<Snapshot>,
    /// Keeps config streams open.
    streams: Mutex<Vec<mpsc::UnboundedSender<Result<ConfigUpdate, Status>>>>,
    events: mpsc::UnboundedSender<(Option<String>, UsageEvent)>,
    pub reports: AtomicU64,
    /// Next Report calls answered UNAVAILABLE.
    pub fail: AtomicU32,
}

/// Decrements `counter` if positive; whether it did.
fn take_one(counter: &AtomicU32) -> bool {
    let mut n = counter.load(Ordering::SeqCst);
    while n > 0 {
        match counter.compare_exchange(n, n - 1, Ordering::SeqCst, Ordering::SeqCst) {
            Ok(_) => return true,
            Err(current) => n = current,
        }
    }
    false
}

struct Updates(mpsc::UnboundedReceiver<Result<ConfigUpdate, Status>>);
impl Stream for Updates {
    type Item = Result<ConfigUpdate, Status>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_recv(cx)
    }
}

struct StreamImpl(Arc<CpState>);
impl tonic::server::StreamingService<GatewayMessage> for StreamImpl {
    type Response = ConfigUpdate;
    type ResponseStream = Updates;
    type Future = BoxFuture<tonic::Response<Updates>, Status>;

    fn call(&mut self, request: tonic::Request<tonic::Streaming<GatewayMessage>>) -> Self::Future {
        let state = self.0.clone();
        Box::pin(async move {
            let mut incoming = request.into_inner();
            tokio::spawn(async move { while let Ok(Some(_)) = incoming.message().await {} });
            let (tx, rx) = mpsc::unbounded_channel();
            if let Some(snapshot) = &state.snapshot {
                tx.send(Ok(ConfigUpdate {
                    update: Some(Update::Full(snapshot.clone())),
                    target_digest: b"usage-test-digest".to_vec(),
                }))
                .unwrap();
            }
            state.streams.lock().unwrap().push(tx);
            Ok(tonic::Response::new(Updates(rx)))
        })
    }
}

struct ReportImpl(Arc<CpState>);
impl tonic::server::UnaryService<ReportRequest> for ReportImpl {
    type Response = ReportResponse;
    type Future = BoxFuture<tonic::Response<ReportResponse>, Status>;

    fn call(&mut self, request: tonic::Request<ReportRequest>) -> Self::Future {
        let state = self.0.clone();
        Box::pin(async move {
            if take_one(&state.fail) {
                return Err(Status::unavailable("control plane down"));
            }
            let authorization = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned);
            let request = request.into_inner();
            state.reports.fetch_add(1, Ordering::SeqCst);
            let accepted = request.events.len() as u32;
            for event in request.events {
                let _ = state.events.send((authorization.clone(), event));
            }
            Ok(tonic::Response::new(ReportResponse { accepted }))
        })
    }
}

macro_rules! grpc_service {
    ($name:ident, $service:literal, $path:literal, $method:ident, $imp:ident) => {
        #[derive(Clone)]
        struct $name(Arc<CpState>);
        impl NamedService for $name {
            const NAME: &'static str = $service;
        }
        impl Service<http::Request<tonic::body::Body>> for $name {
            type Response = http::Response<tonic::body::Body>;
            type Error = Infallible;
            type Future = BoxFuture<Self::Response, Self::Error>;

            fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
                Poll::Ready(Ok(()))
            }

            fn call(&mut self, req: http::Request<tonic::body::Body>) -> Self::Future {
                let state = self.0.clone();
                Box::pin(async move {
                    if req.uri().path() != $path {
                        return Ok(Status::unimplemented("unknown method").into_http());
                    }
                    let mut grpc = tonic::server::Grpc::new(tonic_prost::ProstCodec::default());
                    Ok(grpc.$method($imp(state), req).await)
                })
            }
        }
    };
}
grpc_service!(
    ConfigSvc,
    "lumewarp.config.v1.ConfigService",
    "/lumewarp.config.v1.ConfigService/Stream",
    streaming,
    StreamImpl
);
grpc_service!(
    UsageSvc,
    "lumewarp.usage.v1.UsageService",
    "/lumewarp.usage.v1.UsageService/Report",
    unary,
    ReportImpl
);

pub struct FakeCp {
    pub addr: SocketAddr,
    pub state: Arc<CpState>,
    pub events: mpsc::UnboundedReceiver<(Option<String>, UsageEvent)>,
    task: JoinHandle<()>,
}

impl FakeCp {
    pub async fn start(snapshot: Snapshot) -> Self {
        Self::serving(Some(snapshot)).await
    }

    /// A control plane that accepts streams and reports but has no config yet.
    pub async fn without_config() -> Self {
        Self::serving(None).await
    }

    async fn serving(snapshot: Option<Snapshot>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, events) = mpsc::unbounded_channel();
        let state = Arc::new(CpState {
            snapshot,
            streams: Mutex::new(Vec::new()),
            events: tx,
            reports: AtomicU64::new(0),
            fail: AtomicU32::new(0),
        });
        let (config, usage) = (ConfigSvc(state.clone()), UsageSvc(state.clone()));
        let task = tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(config)
                .add_service(usage)
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await;
        });
        Self {
            addr,
            state,
            events,
            task,
        }
    }

    /// The next reported event (reports are batched up to 1 s apart).
    pub async fn event(&mut self) -> UsageEvent {
        let (authorization, event) =
            tokio::time::timeout(Duration::from_secs(10), self.events.recv())
                .await
                .expect("usage event within 10 s")
                .expect("fake control plane stopped");
        assert_eq!(authorization.as_deref(), Some("Bearer gw-secret"));
        event
    }

    /// No further event arrives within two flush intervals.
    pub async fn no_more_events(&mut self) {
        if let Ok(Some((_, event))) =
            tokio::time::timeout(Duration::from_millis(2200), self.events.recv()).await
        {
            panic!("unexpected extra usage event: {event:?}");
        }
    }
}

impl Drop for FakeCp {
    fn drop(&mut self) {
        self.task.abort();
    }
}
