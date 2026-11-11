//! Server-Sent Events parsing and reconnect backoff for the agent's live stream. Pure code, so it
//! is unit-tested on any platform.

use std::time::Duration;

/// Largest single event we accept (the server's events are well under 2 KB).
pub const MAX_EVENT_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub event: String,
    pub data: String,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("stream event too large")]
pub struct TooLarge;

/// Incremental parser: feed it bytes as they arrive, get complete events back.
#[derive(Default)]
pub struct Parser {
    buffer: Vec<u8>,
    event: String,
    data: Vec<String>,
}

impl Parser {
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Event>, TooLarge> {
        self.buffer.extend_from_slice(chunk);
        let mut out = Vec::new();
        while let Some(pos) = self.buffer.iter().position(|&b| b == b'\n') {
            let mut line: Vec<u8> = self.buffer.drain(..=pos).collect();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            let line = String::from_utf8_lossy(&line).into_owned();
            if let Some(ev) = self.line(&line) {
                out.push(ev);
            }
        }
        let pending = self.buffer.len() + self.data.iter().map(String::len).sum::<usize>();
        if pending > MAX_EVENT_BYTES {
            *self = Self::default();
            return Err(TooLarge);
        }
        Ok(out)
    }

    fn line(&mut self, line: &str) -> Option<Event> {
        if line.is_empty() {
            let event = std::mem::take(&mut self.event);
            let data = std::mem::take(&mut self.data);
            if event.is_empty() && data.is_empty() {
                return None;
            }
            return Some(Event {
                event: if event.is_empty() {
                    "message".into()
                } else {
                    event
                },
                data: data.join("\n"),
            });
        }
        if line.starts_with(':') {
            return None; // comment (the server's 15 s heartbeat)
        }
        let (field, value) = match line.split_once(':') {
            Some((f, v)) => (f, v.strip_prefix(' ').unwrap_or(v)),
            None => (line, ""),
        };
        match field {
            "event" => self.event = value.to_owned(),
            "data" => self.data.push(value.to_owned()),
            _ => {} // id, retry and unknown fields are not needed
        }
        None
    }
}

/// Exponential backoff with jitter: 0.5–1 × min(30 s, 1 s × 2^attempt), like the web client.
/// `jitter` is in [0, 1).
pub fn backoff(attempt: u32, jitter: f64) -> Duration {
    let base = 1_000u64.saturating_mul(1u64 << attempt.min(15)).min(30_000) as f64;
    Duration::from_millis((base / 2.0 + jitter.clamp(0.0, 1.0) * base / 2.0) as u64)
}

/// Cheap jitter source; it only spreads reconnects, so it need not be cryptographic.
pub fn jitter() -> f64 {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    f64::from(nanos % 1_000_000) / 1_000_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_events_across_chunk_boundaries() {
        let mut p = Parser::default();
        assert!(p
            .feed(b"retry: 3000\n\n: hb\n\nid: 7\nevent: tx")
            .unwrap()
            .is_empty());
        let events = p
            .feed(b"n\ndata: {\"a\":1}\r\n\nevent: shift_ended\ndata: {}\n\n")
            .unwrap();
        assert_eq!(
            events,
            vec![
                Event {
                    event: "txn".into(),
                    data: "{\"a\":1}".into()
                },
                Event {
                    event: "shift_ended".into(),
                    data: "{}".into()
                },
            ]
        );
    }

    #[test]
    fn multi_line_data_and_default_event_name() {
        let mut p = Parser::default();
        let events = p.feed(b"data: a\ndata:b\n\n").unwrap();
        assert_eq!(
            events,
            vec![Event {
                event: "message".into(),
                data: "a\nb".into()
            }]
        );
    }

    #[test]
    fn utf8_split_across_chunks() {
        let mut p = Parser::default();
        let text = "event: txn\ndata: {\"amount_text\":\"2,800 ₮\"}\n\n".as_bytes();
        let (a, b) = text.split_at(text.len() - 6);
        assert!(p.feed(a).unwrap().is_empty());
        assert_eq!(p.feed(b).unwrap()[0].data, "{\"amount_text\":\"2,800 ₮\"}");
    }

    #[test]
    fn refuses_oversized_events() {
        let mut p = Parser::default();
        let big = vec![b'x'; MAX_EVENT_BYTES + 1];
        assert_eq!(p.feed(&big), Err(TooLarge));
        assert!(p.feed(b"event: test\ndata: {}\n\n").unwrap().len() == 1);
    }

    #[test]
    fn backoff_grows_and_caps() {
        assert_eq!(backoff(0, 0.0), Duration::from_millis(500));
        assert_eq!(backoff(0, 1.0), Duration::from_millis(1000));
        assert_eq!(backoff(3, 0.0), Duration::from_millis(4000));
        assert_eq!(backoff(10, 0.0), Duration::from_millis(15_000));
        assert_eq!(backoff(99, 1.0), Duration::from_millis(30_000));
        let j = jitter();
        assert!((0.0..1.0).contains(&j));
    }
}
