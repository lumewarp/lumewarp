//! A known-length upstream body, optionally spliced around an upload's model part.
use bytes::Bytes;
use hyper::body::{Body, Frame, SizeHint};
use std::{
    convert::Infallible,
    pin::Pin,
    task::{Context, Poll},
};

pub struct RequestBody {
    chunks: [Option<Bytes>; 3],
    remaining: u64,
}
impl RequestBody {
    pub fn spliced(before: Bytes, replacement: Bytes, after: Bytes) -> Self {
        Self {
            remaining: (before.len() + replacement.len() + after.len()) as u64,
            chunks: [Some(before), Some(replacement), Some(after)],
        }
    }
}
impl From<Bytes> for RequestBody {
    fn from(bytes: Bytes) -> Self {
        Self {
            remaining: bytes.len() as u64,
            chunks: [Some(bytes), None, None],
        }
    }
}
impl Body for RequestBody {
    type Data = Bytes;
    type Error = Infallible;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        for chunk in &mut self.chunks {
            if let Some(bytes) = chunk.take() {
                self.remaining -= bytes.len() as u64;
                return Poll::Ready(Some(Ok(Frame::data(bytes))));
            }
        }
        Poll::Ready(None)
    }
    fn is_end_stream(&self) -> bool {
        self.remaining == 0
    }
    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.remaining)
    }
}
