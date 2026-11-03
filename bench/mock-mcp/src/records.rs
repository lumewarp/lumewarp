//! Bounded, in-memory request recording.

use crate::model::{now_ms, RequestRecord};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Clone, Debug, Default)]
pub struct RecordFilter {
    pub server: Option<String>,
    pub http_method: Option<String>,
    pub method: Option<String>,
    pub tool: Option<String>,
    pub after: Option<u64>,
    pub limit: Option<usize>,
}

#[derive(Debug)]
pub struct RecordLog {
    capacity: usize,
    body_limit: usize,
    enabled: AtomicBool,
    next_seq: AtomicU64,
    inner: Mutex<VecDeque<Arc<Mutex<RequestRecord>>>>,
}

impl RecordLog {
    pub fn new(capacity: usize, body_limit: usize, enabled: bool) -> Self {
        Self {
            capacity,
            body_limit,
            enabled: AtomicBool::new(enabled && capacity > 0),
            next_seq: AtomicU64::new(0),
            inner: Mutex::new(VecDeque::with_capacity(capacity.min(4096))),
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    pub fn set_enabled(&self, enabled: bool) {
        self.enabled
            .store(enabled && self.capacity > 0, Ordering::Relaxed);
    }

    pub fn len(&self) -> usize {
        self.inner.lock().expect("records lock").len()
    }

    /// Start a record. `None` when recording is off, so the caller avoids building it.
    pub fn begin(
        &self,
        server: &str,
        http_method: &str,
        path: &str,
    ) -> Option<Arc<Mutex<RequestRecord>>> {
        if !self.enabled() {
            return None;
        }
        let seq = self.next_seq.fetch_add(1, Ordering::Relaxed);
        let record = Arc::new(Mutex::new(RequestRecord::new(
            seq,
            server.to_string(),
            http_method.to_string(),
            path.to_string(),
        )));
        let mut inner = self.inner.lock().expect("records lock");
        if inner.len() == self.capacity {
            inner.pop_front();
        }
        inner.push_back(record.clone());
        Some(record)
    }

    pub fn body(&self, bytes: &[u8]) -> (String, bool) {
        let limit = self.body_limit;
        if bytes.len() <= limit {
            return (String::from_utf8_lossy(bytes).into_owned(), false);
        }
        // Cut on a UTF-8 boundary so the recorded body stays a string, not a repair artefact.
        let mut end = limit;
        while end > 0 && std::str::from_utf8(&bytes[..end]).is_err() {
            end -= 1;
        }
        (String::from_utf8_lossy(&bytes[..end]).into_owned(), true)
    }

    pub fn snapshot(&self, filter: &RecordFilter) -> Vec<RequestRecord> {
        let inner = self.inner.lock().expect("records lock");
        let limit = filter.limit.unwrap_or(usize::MAX);
        let mut out = Vec::new();
        for record in inner.iter() {
            let record = record.lock().expect("record lock").clone();
            if let Some(server) = &filter.server {
                if &record.server != server {
                    continue;
                }
            }
            if let Some(method) = &filter.http_method {
                if !record.http_method.eq_ignore_ascii_case(method) {
                    continue;
                }
            }
            if let Some(method) = &filter.method {
                if record.method.as_deref() != Some(method.as_str()) {
                    continue;
                }
            }
            if let Some(tool) = &filter.tool {
                if record.tool.as_deref() != Some(tool.as_str()) {
                    continue;
                }
            }
            if let Some(after) = filter.after {
                if record.seq <= after {
                    continue;
                }
            }
            out.push(record);
            if out.len() >= limit {
                break;
            }
        }
        out
    }

    pub fn clear(&self, server: Option<&str>) -> usize {
        let mut inner = self.inner.lock().expect("records lock");
        let before = inner.len();
        match server {
            None => inner.clear(),
            Some(server) => {
                inner.retain(|record| record.lock().map(|r| r.server != server).unwrap_or(false))
            }
        }
        before - inner.len()
    }
}

pub fn finish(
    record: &Option<Arc<Mutex<RequestRecord>>>,
    outcome: &str,
    status: u16,
    content_type: &str,
    result_type: Option<&str>,
    fault: Option<&str>,
) {
    if let Some(record) = record {
        if let Ok(mut record) = record.lock() {
            record.completed_at_ms = Some(now_ms());
            record.status = Some(status);
            record.content_type = Some(content_type.to_string());
            record.outcome = Some(outcome.to_string());
            record.result_type = result_type.map(str::to_string);
            record.fault = fault.map(str::to_string);
            record.completed_after_disconnect = record.disconnected && !record.cancelled;
        }
    }
}

pub fn mark_disconnected(record: &Option<Arc<Mutex<RequestRecord>>>) {
    if let Some(record) = record {
        if let Ok(mut record) = record.lock() {
            record.disconnected = true;
        }
    }
}

pub fn mark_cancelled(record: &Option<Arc<Mutex<RequestRecord>>>, by: &str) {
    if let Some(record) = record {
        if let Ok(mut record) = record.lock() {
            record.cancelled = true;
            record.cancelled_by = Some(by.to_string());
            record.completed_at_ms = Some(now_ms());
        }
    }
}

pub fn annotate(record: &Option<Arc<Mutex<RequestRecord>>>, f: impl FnOnce(&mut RequestRecord)) {
    if let Some(record) = record {
        if let Ok(mut record) = record.lock() {
            f(&mut record);
        }
    }
}
