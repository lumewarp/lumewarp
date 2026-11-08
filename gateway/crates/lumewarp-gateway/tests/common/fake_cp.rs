//! In-process fake `ConfigService`: a hand-written tonic service (the proto
//! crate only generates clients). Each accepted stream becomes a `Conn` the
//! test drives message by message.

#![allow(dead_code)]

use std::{
    convert::Infallible,
    net::SocketAddr,
    pin::Pin,
    task::{Context, Poll},
};

use lumewarp_proto::config::v1::{
    gateway_message::Msg, CatalogReport, ConfigUpdate, GatewayMessage, HealthReport,
};
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_stream::{wrappers::TcpListenerStream, Stream};
use tonic::{
    codegen::{http, BoxFuture, Service},
    server::NamedService,
    Request, Response, Status, Streaming,
};

pub struct Conn {
    /// Metadata `authorization` header the gateway presented.
    pub authorization: Option<String>,
    pub inbound: mpsc::UnboundedReceiver<GatewayMessage>,
    /// Dropping it ends the stream from the server side.
    pub outbound: mpsc::UnboundedSender<Result<ConfigUpdate, Status>>,
    /// Health reports `recv` skipped, oldest first.
    pub health: Vec<HealthReport>,
    /// Catalog reports `recv` skipped, oldest first.
    pub catalog: Vec<CatalogReport>,
}

impl Conn {
    pub fn send(&self, update: ConfigUpdate) {
        self.outbound
            .send(Ok(update))
            .expect("gateway stream closed");
    }

    /// The next Hello, ACK or NACK; health and catalog reports are kept.
    pub async fn recv(&mut self) -> GatewayMessage {
        loop {
            let message = super::bounded(self.inbound.recv())
                .await
                .expect("gateway closed the stream");
            match message.msg {
                Some(Msg::Health(report)) => self.health.push(report),
                Some(Msg::Catalog(report)) => self.catalog.push(report),
                _ => return message,
            }
        }
    }

    /// The next catalog report, including one `recv` already skipped.
    pub async fn recv_catalog(&mut self) -> CatalogReport {
        loop {
            if !self.catalog.is_empty() {
                return self.catalog.remove(0);
            }
            let message = super::bounded(self.inbound.recv())
                .await
                .expect("gateway closed the stream");
            if let Some(Msg::Catalog(report)) = message.msg {
                return report;
            }
        }
    }

    /// The next health report, including one `recv` already skipped.
    pub async fn recv_health(&mut self) -> HealthReport {
        loop {
            if !self.health.is_empty() {
                return self.health.remove(0);
            }
            let message = super::bounded(self.inbound.recv())
                .await
                .expect("gateway closed the stream");
            if let Some(Msg::Health(report)) = message.msg {
                return report;
            }
        }
    }
}

type Conns = mpsc::UnboundedSender<Conn>;

#[derive(Clone)]
struct Svc(Conns);

impl NamedService for Svc {
    const NAME: &'static str = "lumewarp.config.v1.ConfigService";
}

struct Updates(mpsc::UnboundedReceiver<Result<ConfigUpdate, Status>>);
impl Stream for Updates {
    type Item = Result<ConfigUpdate, Status>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_recv(cx)
    }
}

struct StreamImpl(Conns);
impl tonic::server::StreamingService<GatewayMessage> for StreamImpl {
    type Response = ConfigUpdate;
    type ResponseStream = Updates;
    type Future = BoxFuture<Response<Updates>, Status>;

    fn call(&mut self, request: Request<Streaming<GatewayMessage>>) -> Self::Future {
        let conns = self.0.clone();
        Box::pin(async move {
            let authorization = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned);
            let mut incoming = request.into_inner();
            let (in_tx, in_rx) = mpsc::unbounded_channel();
            let (out_tx, out_rx) = mpsc::unbounded_channel();
            tokio::spawn(async move {
                while let Ok(Some(message)) = incoming.message().await {
                    if in_tx.send(message).is_err() {
                        break;
                    }
                }
            });
            let _ = conns.send(Conn {
                authorization,
                inbound: in_rx,
                outbound: out_tx,
                health: Vec::new(),
                catalog: Vec::new(),
            });
            Ok(Response::new(Updates(out_rx)))
        })
    }
}

impl Service<http::Request<tonic::body::Body>> for Svc {
    type Response = http::Response<tonic::body::Body>;
    type Error = Infallible;
    type Future = BoxFuture<Self::Response, Self::Error>;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, req: http::Request<tonic::body::Body>) -> Self::Future {
        let conns = self.0.clone();
        Box::pin(async move {
            if req.uri().path() != "/lumewarp.config.v1.ConfigService/Stream" {
                return Ok(Status::unimplemented("unknown method").into_http());
            }
            let mut grpc = tonic::server::Grpc::new(tonic_prost::ProstCodec::default());
            Ok(grpc.streaming(StreamImpl(conns), req).await)
        })
    }
}

pub struct FakeCp {
    pub addr: SocketAddr,
    conns: mpsc::UnboundedReceiver<Conn>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
}

impl FakeCp {
    pub async fn start() -> Self {
        Self::start_on(None).await
    }

    pub async fn start_on(addr: Option<SocketAddr>) -> Self {
        let listener = TcpListener::bind(addr.unwrap_or(([127, 0, 0, 1], 0).into()))
            .await
            .expect("bind fake control plane");
        let addr = listener.local_addr().unwrap();
        let (tx, conns) = mpsc::unbounded_channel();
        let (stop, stopped) = oneshot::channel::<()>();
        let task = tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(Svc(tx))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = stopped.await;
                })
                .await;
        });
        Self {
            addr,
            conns,
            stop: Some(stop),
            task: Some(task),
        }
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// Waits for the next stream the gateway opens.
    pub async fn accept(&mut self) -> Conn {
        super::bounded(self.conns.recv())
            .await
            .expect("fake control plane stopped")
    }

    /// [`Self::accept`] with its own deadline: a debug build compiles the WASM
    /// plugins at startup, which takes longer than the shared 5 s bound.
    pub async fn accept_within(&mut self, timeout: std::time::Duration) -> Conn {
        tokio::time::timeout(timeout, self.conns.recv())
            .await
            .expect("the gateway did not open its config stream")
            .expect("fake control plane stopped")
    }

    /// Stops listening; open `Conn`s must be dropped by the caller first.
    pub async fn shutdown(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(task) = self.task.take() {
            let _ = super::bounded(task).await;
        }
    }
}

impl Drop for FakeCp {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}
