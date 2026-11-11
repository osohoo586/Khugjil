//! The live stream (PLAN-PHASE2 §1, decision 2): Rust holds `GET /api/v1/stream` with the bearer
//! token and forwards each event to the main window, which runs the same code as the web cashier.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::StreamExt;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::watch;

use crate::api::{ApiClient, StreamOpen};
use crate::sse::{backoff, jitter, Event, Parser};

pub const STREAM_EVENT: &str = "khj://stream";
/// The server sends a heartbeat every 15 s; three missed heartbeats mean the connection is dead.
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(45);

/// What the stream reports besides events: used for the tray status and the updater.
pub trait StreamObserver: Send + Sync + 'static {
    fn connected(&self, ok: bool);
    fn update_required(&self);
}

#[derive(Default)]
pub struct StreamControl {
    stop: Mutex<Option<watch::Sender<bool>>>,
}

impl StreamControl {
    pub fn start(&self, app: AppHandle, api: Arc<ApiClient>, observer: Arc<dyn StreamObserver>) {
        self.stop();
        let (tx, rx) = watch::channel(false);
        if let Ok(mut slot) = self.stop.lock() {
            *slot = Some(tx);
        }
        tauri::async_runtime::spawn(run(app, api, observer, rx));
    }

    pub fn stop(&self) {
        if let Ok(mut slot) = self.stop.lock() {
            if let Some(tx) = slot.take() {
                let _ = tx.send(true);
            }
        }
    }
}

enum Outcome {
    Retry,
    Revoked,
}

/// Map an SSE event to the payload the webview expects (`StreamEvent` in platform-agent.ts).
pub fn to_payload(ev: &Event) -> Option<Value> {
    match ev.event.as_str() {
        "txn" | "test" => Some(json!({"kind": ev.event, "data": ev.data})),
        "shift_ended" | "settings_changed" | "device_revoked" => Some(json!({"kind": ev.event})),
        _ => None,
    }
}

async fn run(
    app: AppHandle,
    api: Arc<ApiClient>,
    observer: Arc<dyn StreamObserver>,
    mut stop: watch::Receiver<bool>,
) {
    let emit = |payload: Value| {
        let _ = app.emit_to("main", STREAM_EVENT, payload);
    };
    let mut attempt = 0u32;
    let mut ever_opened = false;
    loop {
        let outcome = tokio::select! {
            _ = stop.changed() => return,
            o = session(&api, &*observer, &emit, &mut attempt, &mut ever_opened) => o,
        };
        match outcome {
            Outcome::Revoked => {
                api.forget_token();
                observer.connected(false);
                emit(json!({"kind": "device_revoked"}));
                return;
            }
            Outcome::Retry => {
                observer.connected(false);
                emit(json!({"kind": "status", "connected": false}));
                let delay = backoff(attempt, jitter());
                attempt = attempt.saturating_add(1);
                tokio::select! {
                    _ = stop.changed() => return,
                    _ = tokio::time::sleep(delay) => {}
                }
            }
        }
    }
}

async fn session(
    api: &ApiClient,
    observer: &dyn StreamObserver,
    emit: &impl Fn(Value),
    attempt: &mut u32,
    ever_opened: &mut bool,
) -> Outcome {
    let resp = match api.open_stream().await {
        StreamOpen::Open(resp) => resp,
        StreamOpen::Unauthorized => return Outcome::Revoked,
        StreamOpen::UpdateRequired => {
            observer.update_required();
            return Outcome::Retry;
        }
        StreamOpen::Failed => return Outcome::Retry,
    };
    if *ever_opened && *attempt > 0 {
        emit(json!({"kind": "reconnected"}));
    }
    *ever_opened = true;
    *attempt = 0;
    observer.connected(true);
    emit(json!({"kind": "status", "connected": true}));

    let mut parser = Parser::default();
    let mut body = resp.bytes_stream();
    loop {
        let chunk = match tokio::time::timeout(HEARTBEAT_TIMEOUT, body.next()).await {
            Ok(Some(Ok(chunk))) => chunk,
            _ => return Outcome::Retry, // heartbeat missed, stream ended or failed
        };
        let Ok(events) = parser.feed(&chunk) else {
            return Outcome::Retry;
        };
        for ev in events {
            if ev.event == "device_revoked" {
                return Outcome::Revoked;
            }
            if let Some(payload) = to_payload(&ev) {
                emit(payload);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(event: &str, data: &str) -> Event {
        Event {
            event: event.into(),
            data: data.into(),
        }
    }

    #[test]
    fn forwards_only_known_events() {
        assert_eq!(
            to_payload(&ev("txn", "{}")),
            Some(json!({"kind": "txn", "data": "{}"}))
        );
        assert_eq!(
            to_payload(&ev("test", "{}")),
            Some(json!({"kind": "test", "data": "{}"}))
        );
        assert_eq!(
            to_payload(&ev("shift_ended", "{}")),
            Some(json!({"kind": "shift_ended"}))
        );
        assert_eq!(
            to_payload(&ev("settings_changed", "{}")),
            Some(json!({"kind": "settings_changed"}))
        );
        assert_eq!(to_payload(&ev("message", "{}")), None);
        assert_eq!(to_payload(&ev("status", "{}")), None); // the server cannot fake our own kinds
        assert_eq!(to_payload(&ev("reconnected", "")), None);
    }
}
