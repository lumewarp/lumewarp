//! A named MCP server instance: immutable state for the hot path, mutable sessions
//! and the in-flight table used only by legacy cancellation.

use crate::model::RequestRecord;
use crate::model::{Fault, FaultKind, InstanceConfig, SessionMode, ToolConfig};
use crate::state::InstanceState;
use arc_swap::ArcSwap;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
use tokio_util::sync::CancellationToken;

pub struct Instance {
    pub name: String,
    pub(crate) state: ArcSwap<InstanceState>,
    pub(crate) sessions: Mutex<HashMap<String, SessionInfo>>,
    pub(crate) in_flight: Mutex<HashMap<String, Weak<InFlight>>>,
    pub(crate) session_expire_armed: AtomicBool,
    update: Mutex<()>,
}

#[derive(Clone, Debug)]
pub struct SessionInfo {
    pub initialized: bool,
}

impl Instance {
    pub fn new(config: InstanceConfig) -> Result<Arc<Self>, String> {
        let armed = config
            .fault
            .as_ref()
            .is_some_and(|fault| fault.kind == FaultKind::Session404Once);
        let state = InstanceState::build(config.clone())?;
        Ok(Arc::new(Self {
            name: config.name,
            state: ArcSwap::from_pointee(state),
            sessions: Mutex::new(HashMap::new()),
            in_flight: Mutex::new(HashMap::new()),
            session_expire_armed: AtomicBool::new(armed),
            update: Mutex::new(()),
        }))
    }

    pub fn snapshot(&self) -> Arc<InstanceState> {
        self.state.load_full()
    }

    pub fn config(&self) -> InstanceConfig {
        self.snapshot().config.clone()
    }

    pub fn update<F>(&self, mutate: F) -> Result<Arc<InstanceState>, String>
    where
        F: FnOnce(&mut InstanceConfig) -> Result<(), String>,
    {
        let _guard = self.update.lock().expect("instance update lock");
        let mut config = self.snapshot().config.clone();
        mutate(&mut config)?;
        let state = InstanceState::build(config)?;
        let armed = state
            .config
            .fault
            .as_ref()
            .is_some_and(|fault| fault.kind == FaultKind::Session404Once);
        if armed {
            self.session_expire_armed.store(true, Ordering::SeqCst);
        }
        self.state.store(Arc::new(state));
        Ok(self.snapshot())
    }

    pub fn replace_tools(&self, tools: Vec<ToolConfig>) -> Result<(), String> {
        self.update(|config| {
            config.tools = tools;
            Ok(())
        })
        .map(|_| ())
    }

    pub fn set_fault(&self, fault: Option<Fault>) -> Result<(), String> {
        if let Some(fault) = &fault {
            crate::model::validate_fault(fault)?;
        }
        self.update(|config| {
            config.fault = fault.clone();
            Ok(())
        })?;
        if fault
            .as_ref()
            .is_some_and(|fault| fault.kind == FaultKind::Session404Once)
        {
            self.session_expire_armed.store(true, Ordering::SeqCst);
        }
        Ok(())
    }

    pub fn session_mode(&self) -> SessionMode {
        self.snapshot().config.session
    }

    pub fn new_session(&self) -> Option<String> {
        if self.session_mode() == SessionMode::Off {
            return None;
        }
        let id = uuid::Uuid::new_v4().to_string();
        self.sessions
            .lock()
            .expect("sessions lock")
            .insert(id.clone(), SessionInfo { initialized: false });
        Some(id)
    }

    pub fn session_exists(&self, id: &str) -> bool {
        self.sessions
            .lock()
            .expect("sessions lock")
            .contains_key(id)
    }

    pub fn mark_initialized(&self, id: &str) {
        if let Some(session) = self.sessions.lock().expect("sessions lock").get_mut(id) {
            session.initialized = true;
        }
    }

    pub fn expire_session(&self, id: &str) -> bool {
        self.sessions
            .lock()
            .expect("sessions lock")
            .remove(id)
            .is_some()
    }

    /// Arm the next session-bearing request to answer 404 once.
    pub fn arm_session_expiry(&self) {
        self.session_expire_armed.store(true, Ordering::SeqCst);
    }

    pub fn take_session_expiry(&self) -> bool {
        self.session_expire_armed.swap(false, Ordering::SeqCst)
    }

    pub fn register_in_flight(&self, key: String, flight: &Arc<InFlight>) {
        self.in_flight
            .lock()
            .expect("in-flight lock")
            .insert(key, Arc::downgrade(flight));
    }

    pub fn cancel_in_flight(&self, key: &str) -> bool {
        let flight = self
            .in_flight
            .lock()
            .expect("in-flight lock")
            .get(key)
            .and_then(Weak::upgrade);
        match flight {
            Some(flight) => {
                flight.cancel("notification");
                true
            }
            None => false,
        }
    }

    fn remove_in_flight(&self, key: &str, ptr: *const InFlight) {
        let mut table = self.in_flight.lock().expect("in-flight lock");
        if let Some(weak) = table.get(key) {
            if std::ptr::eq(weak.as_ptr(), ptr) {
                table.remove(key);
            }
        }
    }
}

pub struct InFlight {
    key: String,
    pub record: Option<Arc<Mutex<RequestRecord>>>,
    token: CancellationToken,
    finished: AtomicBool,
    instance: Arc<Instance>,
}

impl InFlight {
    pub fn new(
        key: String,
        record: Option<Arc<Mutex<RequestRecord>>>,
        instance: Arc<Instance>,
    ) -> Arc<Self> {
        Arc::new(Self {
            key,
            record,
            token: CancellationToken::new(),
            finished: AtomicBool::new(false),
            instance,
        })
    }

    pub fn token(&self) -> CancellationToken {
        self.token.clone()
    }

    pub fn is_finished(&self) -> bool {
        self.finished.load(Ordering::SeqCst)
    }

    pub fn cancel(&self, by: &str) {
        if !self.is_finished() {
            crate::records::mark_cancelled(&self.record, by);
            self.token.cancel();
        }
    }

    pub fn finish(
        &self,
        outcome: &str,
        status: Option<u16>,
        content_type: Option<&str>,
        result_type: Option<&str>,
        fault: Option<&str>,
    ) {
        if self.finished.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(record) = &self.record {
            if let Ok(mut record) = record.lock() {
                record.completed_at_ms = Some(crate::model::now_ms());
                record.outcome = Some(outcome.to_string());
                if let Some(status) = status {
                    record.status = Some(status);
                }
                if let Some(content_type) = content_type {
                    record.content_type = Some(content_type.to_string());
                }
                record.result_type = result_type.map(str::to_string);
                record.fault = fault.map(str::to_string);
                record.completed_after_disconnect = record.disconnected && !record.cancelled;
            }
        }
    }

    pub fn finish_cancelled(&self) {
        if self.finished.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(record) = &self.record {
            if let Ok(mut record) = record.lock() {
                if !record.cancelled {
                    record.cancelled = true;
                    record.cancelled_by = Some("notification".into());
                }
                record.completed_at_ms = Some(crate::model::now_ms());
                record.outcome = Some("cancelled".into());
                record.completed_after_disconnect = false;
            }
        }
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        self.instance
            .remove_in_flight(&self.key, self as *const InFlight);
    }
}
