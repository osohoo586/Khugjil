//! Allowlist for `api_request` (PLAN-PHASE2 §5, I6). The webview may only reach these cashier API
//! endpoints, with these methods and these exact query shapes. Everything else is refused before
//! any network traffic, so a UI bug cannot use the device token for anything else.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
}

impl Method {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "GET" => Some(Self::Get),
            "POST" => Some(Self::Post),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    Device,
    Pair,
    ShiftStart,
    ShiftEnd,
    ShiftActivity,
    Transactions,
    Verify,
    Ack,
    SummaryToday,
}

impl Route {
    /// Requests that carry a JSON body.
    pub fn takes_body(self) -> bool {
        matches!(self, Self::Pair | Self::ShiftStart)
    }
}

const PREFIX: &str = "/api/v1/";

/// Match `method` + `path` (path plus optional query, exactly as the cashier `Api` builds it).
pub fn match_route(method: &str, path: &str) -> Option<Route> {
    let method = Method::parse(method)?;
    if path.len() > 200 || !path.is_ascii() {
        return None;
    }
    // No encoded characters, fragments, backslashes, dot segments or empty segments anywhere.
    if path.contains(['%', '#', '\\', ' ']) || path.contains("..") || path.contains("//") {
        return None;
    }
    let rest = path.strip_prefix(PREFIX)?;
    let (route, query) = match rest.split_once('?') {
        Some((r, q)) => (r, Some(q)),
        None => (rest, None),
    };
    let found = match (method, route) {
        (Method::Get, "device") => Route::Device,
        (Method::Post, "pair") => Route::Pair,
        (Method::Post, "shift/start") => Route::ShiftStart,
        (Method::Post, "shift/end") => Route::ShiftEnd,
        (Method::Post, "shift/activity") => Route::ShiftActivity,
        (Method::Get, "transactions") => Route::Transactions,
        (Method::Get, "verify") => Route::Verify,
        (Method::Get, "summary/today") => Route::SummaryToday,
        (Method::Post, r) if is_ack(r) => Route::Ack,
        _ => return None,
    };
    let query_ok = match (found, query) {
        (_, None) => found != Route::Verify,
        (Route::Transactions, Some(q)) => q
            .strip_prefix("since_seq=")
            .is_some_and(|n| is_digits(n, 1, 18)),
        (Route::Verify, Some(q)) => q.strip_prefix("amount=").is_some_and(is_amount),
        _ => false,
    };
    query_ok.then_some(found)
}

fn is_ack(route: &str) -> bool {
    route
        .strip_prefix("transactions/")
        .and_then(|r| r.strip_suffix("/ack"))
        .is_some_and(is_uuid)
}

fn is_uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

fn is_digits(s: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}

/// What `parseAmountInput` produces: digits with an optional 1–2 digit fraction.
fn is_amount(s: &str) -> bool {
    match s.split_once('.') {
        Some((whole, frac)) => is_digits(whole, 1, 15) && is_digits(frac, 1, 2),
        None => is_digits(s, 1, 15),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "0b9c6f2e-1d2a-4f5b-9c3d-7e8f9a0b1c2d";

    #[test]
    fn allows_exactly_the_cashier_endpoints() {
        let ok = [
            ("GET", "/api/v1/device".to_string(), Route::Device),
            ("POST", "/api/v1/pair".to_string(), Route::Pair),
            ("POST", "/api/v1/shift/start".to_string(), Route::ShiftStart),
            ("POST", "/api/v1/shift/end".to_string(), Route::ShiftEnd),
            (
                "POST",
                "/api/v1/shift/activity".to_string(),
                Route::ShiftActivity,
            ),
            (
                "GET",
                "/api/v1/transactions".to_string(),
                Route::Transactions,
            ),
            (
                "GET",
                "/api/v1/transactions?since_seq=42".to_string(),
                Route::Transactions,
            ),
            (
                "GET",
                "/api/v1/verify?amount=2800".to_string(),
                Route::Verify,
            ),
            (
                "GET",
                "/api/v1/verify?amount=2800.5".to_string(),
                Route::Verify,
            ),
            (
                "GET",
                "/api/v1/summary/today".to_string(),
                Route::SummaryToday,
            ),
            ("POST", format!("/api/v1/transactions/{ID}/ack"), Route::Ack),
        ];
        for (m, p, r) in ok {
            assert_eq!(match_route(m, &p), Some(r), "{m} {p}");
        }
    }

    #[test]
    fn refuses_everything_else() {
        let bad = [
            ("GET", "/api/v1/stream".to_string()), // the stream is held by Rust only
            ("POST", "/api/v1/push/subscribe".to_string()),
            ("DELETE", "/api/v1/push/subscribe".to_string()),
            ("POST", "/api/v1/invite/accept".to_string()),
            ("POST", "/hooks/cf".to_string()),
            ("GET", "/api/v1/openapi.json".to_string()),
            ("POST", "/api/v1/device".to_string()),
            ("GET", "/api/v1/pair".to_string()),
            ("PUT", "/api/v1/shift/start".to_string()),
            ("get", "/api/v1/device".to_string()),
            ("GET", "/api/v1/device?x=1".to_string()),
            ("GET", "/api/v1/verify".to_string()),
            ("GET", "/api/v1/verify?amount=".to_string()),
            ("GET", "/api/v1/verify?amount=1e9".to_string()),
            ("GET", "/api/v1/verify?amount=2800&x=1".to_string()),
            ("GET", "/api/v1/verify?amount=2,800".to_string()),
            ("GET", "/api/v1/transactions?since_seq=-1".to_string()),
            (
                "GET",
                "/api/v1/transactions?since_seq=1&since_seq=2".to_string(),
            ),
            ("POST", "/api/v1/transactions/not-a-uuid/ack".to_string()),
            ("POST", format!("/api/v1/transactions/{ID}/ack/x")),
            ("POST", format!("/api/v1/transactions/{ID}%2F../ack")),
            ("GET", "/api/v1/../v1/device".to_string()),
            ("GET", "/api/v1//device".to_string()),
            ("GET", "/api/v1/device#x".to_string()),
            ("GET", "/api/v1\\device".to_string()),
            ("GET", "https://evil.example/api/v1/device".to_string()),
            ("GET", "//evil.example/api/v1/device".to_string()),
            ("GET", "/api/v2/device".to_string()),
            ("GET", "/api/v1/devicé".to_string()),
            (
                "GET",
                format!("/api/v1/transactions?since_seq={}", "1".repeat(300)),
            ),
        ];
        for (m, p) in bad {
            assert_eq!(match_route(m, &p), None, "{m} {p}");
        }
    }

    #[test]
    fn only_pair_and_shift_start_take_a_body() {
        assert!(Route::Pair.takes_body());
        assert!(Route::ShiftStart.takes_body());
        assert!(!Route::Ack.takes_body());
        assert!(!Route::ShiftEnd.takes_body());
    }
}
