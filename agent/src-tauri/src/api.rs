//! HTTPS client for the cashier API (PLAN-PHASE2 §1, decision 1). Rust adds the bearer token; the
//! webview gets only status codes and JSON bodies, never the token.

use std::sync::Arc;
use std::time::Duration;

use reqwest::header::{HeaderMap, ACCEPT};
use reqwest::{Client, Response, StatusCode};
use serde::Serialize;
use serde_json::{json, Map, Value};
use zeroize::Zeroizing;

use crate::config::{base_url, VERSION};
use crate::creds::{valid_token, CredentialStore};
use crate::routes::{match_route, Method, Route};

pub const AGENT_HEADER: &str = "x-khj-agent";
pub const ROTATED_HEADER: &str = "x-khj-device-token";
const MAX_REQUEST_BODY: usize = 16 * 1024;
const MAX_RESPONSE_BODY: usize = 512 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
const UNAUTHORIZED: &str = "Төхөөрөмж холбогдоогүй байна. Админаас холбох код авна уу.";

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ApiError {
    #[error("request not allowed")]
    NotAllowed,
    #[error("network error")]
    Network,
    #[error("credential store unavailable")]
    Store,
}

impl Serialize for ApiError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(match self {
            Self::NotAllowed => "not_allowed",
            Self::Network => "network",
            Self::Store => "store",
        })
    }
}

#[derive(Debug, Serialize, PartialEq)]
pub struct IpcResponse {
    pub status: u16,
    pub body: Value,
}

pub enum StreamOpen {
    Open(Response),
    Unauthorized,
    UpdateRequired,
    Failed,
}

pub struct ApiClient {
    http: Client,
    base: String,
    store: Arc<dyn CredentialStore>,
}

/// Only public web PKI roots (Mozilla's list), not the Windows store: HTTPS interception by
/// antivirus software or a locally installed root fails closed (PLAN-PHASE2 P2-Q10 default).
pub fn http_client() -> reqwest::Result<Client> {
    let roots = webpki_root_certs::TLS_SERVER_ROOT_CERTS
        .iter()
        .filter_map(|der| reqwest::Certificate::from_der(der.as_ref()).ok());
    Client::builder()
        .https_only(true)
        .redirect(reqwest::redirect::Policy::none())
        .tls_certs_only(roots)
        .connect_timeout(Duration::from_secs(10))
        .user_agent(format!("khugjil-agent/{VERSION}"))
        .build()
}

impl ApiClient {
    pub fn new(store: Arc<dyn CredentialStore>) -> reqwest::Result<Self> {
        Ok(Self::with_parts(http_client()?, base_url(), store))
    }

    pub fn with_parts(http: Client, base: String, store: Arc<dyn CredentialStore>) -> Self {
        Self { http, base, store }
    }

    pub fn forget_token(&self) {
        let _ = self.store.delete();
    }

    fn token(&self) -> Result<Option<Zeroizing<String>>, ApiError> {
        self.store.get().map_err(|_| ApiError::Store)
    }

    /// The single entry point for the webview (`api_request`).
    pub async fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> Result<IpcResponse, ApiError> {
        let route = match_route(method, path).ok_or(ApiError::NotAllowed)?;
        let body = check_body(route, body)?;
        if route == Route::Pair {
            return self.pair(body.unwrap_or(Value::Null)).await;
        }
        let Some(token) = self.token()? else {
            return Ok(IpcResponse {
                status: 401,
                body: json!({"error": "unauthorized", "message": UNAUTHORIZED}),
            });
        };
        let method = match Method::parse(method) {
            Some(Method::Post) => reqwest::Method::POST,
            _ => reqwest::Method::GET,
        };
        let mut req = self
            .http
            .request(method, format!("{}{path}", self.base))
            .timeout(REQUEST_TIMEOUT)
            .bearer_auth(token.as_str())
            .header(AGENT_HEADER, VERSION)
            .header(ACCEPT, "application/json");
        if let Some(b) = body {
            req = req.json(&b);
        }
        drop(token);
        let resp = req.send().await.map_err(|_| ApiError::Network)?;
        self.after_response(resp.status(), resp.headers());
        let status = resp.status().as_u16();
        Ok(IpcResponse {
            status,
            body: read_json(resp).await?,
        })
    }

    /// Pairing: the token is taken out of the response and stored; the webview only learns
    /// whether it worked.
    async fn pair(&self, body: Value) -> Result<IpcResponse, ApiError> {
        let code = pair_code(&body).ok_or(ApiError::NotAllowed)?;
        let resp = self
            .http
            .post(format!("{}/api/v1/pair", self.base))
            .timeout(REQUEST_TIMEOUT)
            .header(AGENT_HEADER, VERSION)
            .header(ACCEPT, "application/json")
            .json(&json!({"code": code, "client": "agent"}))
            .send()
            .await
            .map_err(|_| ApiError::Network)?;
        let status = resp.status();
        let mut body = read_json(resp).await?;
        let token = take_token(&mut body);
        if status == StatusCode::OK {
            let token = token.filter(|t| valid_token(t)).ok_or(ApiError::Network)?;
            self.store.set(&token).map_err(|_| ApiError::Store)?;
            return Ok(IpcResponse {
                status: 200,
                body: json!({"paired": true}),
            });
        }
        Ok(IpcResponse {
            status: status.as_u16(),
            body,
        })
    }

    /// Store a rotated token; forget the token when the server no longer accepts it.
    fn after_response(&self, status: StatusCode, headers: &HeaderMap) {
        if let Some(new) = headers.get(ROTATED_HEADER).and_then(|v| v.to_str().ok()) {
            if valid_token(new) {
                let _ = self.store.set(new);
            }
        }
        if status == StatusCode::UNAUTHORIZED {
            self.forget_token();
        }
    }

    pub async fn open_stream(&self) -> StreamOpen {
        let token = match self.token() {
            Ok(Some(t)) => t,
            Ok(None) => return StreamOpen::Unauthorized,
            Err(_) => return StreamOpen::Failed,
        };
        let sent = self
            .http
            .get(format!("{}/api/v1/stream", self.base))
            .bearer_auth(token.as_str())
            .header(AGENT_HEADER, VERSION)
            .header(ACCEPT, "text/event-stream")
            .send()
            .await;
        drop(token);
        let Ok(resp) = sent else {
            return StreamOpen::Failed;
        };
        self.after_response(resp.status(), resp.headers());
        match resp.status() {
            StatusCode::OK => StreamOpen::Open(resp),
            StatusCode::UNAUTHORIZED => StreamOpen::Unauthorized,
            StatusCode::UPGRADE_REQUIRED => StreamOpen::UpdateRequired,
            _ => StreamOpen::Failed,
        }
    }
}

/// Only `pair` and `shift/start` carry a JSON object body; nothing else carries any body.
fn check_body(route: Route, body: Option<Value>) -> Result<Option<Value>, ApiError> {
    match (route.takes_body(), body) {
        (true, Some(v @ Value::Object(_))) => {
            let size = serde_json::to_vec(&v)
                .map_err(|_| ApiError::NotAllowed)?
                .len();
            if size > MAX_REQUEST_BODY {
                return Err(ApiError::NotAllowed);
            }
            Ok(Some(v))
        }
        (false, None | Some(Value::Null)) => Ok(None),
        _ => Err(ApiError::NotAllowed),
    }
}

/// The agent pairs only with the typed short code (`{"code": "XXXX-XXXX-XXXX"}`).
fn pair_code(body: &Value) -> Option<&str> {
    let obj = body.as_object()?;
    let code = obj.get("code")?.as_str()?;
    (obj.len() == 1 && !code.is_empty() && code.len() <= 32).then_some(code)
}

fn take_token(body: &mut Value) -> Option<Zeroizing<String>> {
    match body {
        Value::Object(map) => match map.remove("device_token") {
            Some(Value::String(t)) => Some(Zeroizing::new(t)),
            _ => None,
        },
        _ => None,
    }
}

async fn read_json(resp: Response) -> Result<Value, ApiError> {
    if resp
        .content_length()
        .is_some_and(|n| n > MAX_RESPONSE_BODY as u64)
    {
        return Err(ApiError::Network);
    }
    let bytes = resp.bytes().await.map_err(|_| ApiError::Network)?;
    if bytes.len() > MAX_RESPONSE_BODY {
        return Err(ApiError::Network);
    }
    Ok(serde_json::from_slice(&bytes).unwrap_or_else(|_| Value::Object(Map::new())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::creds::MemoryStore;

    fn client(store: Arc<MemoryStore>) -> ApiClient {
        // Port 9 on localhost: nothing listens, so any request that slips through fails fast.
        ApiClient::with_parts(Client::new(), "https://127.0.0.1:9".into(), store)
    }

    #[tokio::test]
    async fn refuses_unlisted_routes_before_any_network_traffic() {
        let api = client(Arc::new(MemoryStore::default()));
        for (m, p) in [
            ("GET", "/api/v1/stream"),
            ("POST", "/api/v1/push/subscribe"),
            ("GET", "/hooks/cf"),
        ] {
            assert_eq!(
                api.request(m, p, None).await,
                Err(ApiError::NotAllowed),
                "{m} {p}"
            );
        }
    }

    #[tokio::test]
    async fn refuses_unexpected_bodies() {
        let api = client(Arc::new(MemoryStore::default()));
        let r = api
            .request("POST", "/api/v1/shift/end", Some(json!({"x": 1})))
            .await;
        assert_eq!(r, Err(ApiError::NotAllowed));
        let r = api
            .request("POST", "/api/v1/shift/start", Some(json!("text")))
            .await;
        assert_eq!(r, Err(ApiError::NotAllowed));
        let big = json!({"member_id": "x".repeat(MAX_REQUEST_BODY)});
        assert_eq!(
            api.request("POST", "/api/v1/shift/start", Some(big)).await,
            Err(ApiError::NotAllowed)
        );
        for bad in [
            json!({}),
            json!({"token": "t"}),
            json!({"code": ""}),
            json!({"code": "A", "client": "web"}),
            json!({"code": "A".repeat(33)}),
        ] {
            assert_eq!(
                api.request("POST", "/api/v1/pair", Some(bad.clone())).await,
                Err(ApiError::NotAllowed),
                "{bad}"
            );
        }
    }

    #[tokio::test]
    async fn answers_401_locally_when_not_paired() {
        let api = client(Arc::new(MemoryStore::default()));
        let r = api.request("GET", "/api/v1/device", None).await.unwrap();
        assert_eq!(r.status, 401);
        assert_eq!(r.body["error"], "unauthorized");
    }

    #[tokio::test]
    async fn network_failures_are_reported_without_detail() {
        let store = Arc::new(MemoryStore::default());
        store.set(&"t".repeat(43)).unwrap();
        let api = client(store);
        assert_eq!(
            api.request("GET", "/api/v1/device", None).await,
            Err(ApiError::Network)
        );
        assert!(matches!(api.open_stream().await, StreamOpen::Failed));
    }

    #[test]
    fn rotation_header_is_stored_and_401_forgets_the_token() {
        let store = Arc::new(MemoryStore::default());
        store.set(&"a".repeat(43)).unwrap();
        let api = client(store.clone());
        let mut headers = HeaderMap::new();
        headers.insert(ROTATED_HEADER, "b".repeat(43).parse().unwrap());
        api.after_response(StatusCode::OK, &headers);
        assert_eq!(store.get().unwrap().unwrap().as_str(), "b".repeat(43));
        headers.insert(ROTATED_HEADER, "not a token".parse().unwrap());
        api.after_response(StatusCode::OK, &headers);
        assert_eq!(store.get().unwrap().unwrap().as_str(), "b".repeat(43));
        api.after_response(StatusCode::UNAUTHORIZED, &HeaderMap::new());
        assert!(store.get().unwrap().is_none());
    }

    #[test]
    fn the_token_is_removed_from_pairing_responses() {
        let mut body = json!({"paired": true, "device_token": "secret-token"});
        assert_eq!(
            take_token(&mut body).as_deref().map(String::as_str),
            Some("secret-token")
        );
        assert_eq!(body, json!({"paired": true}));
        let mut other = json!({"error": "invalid_code"});
        assert!(take_token(&mut other).is_none());
    }

    #[test]
    fn errors_serialize_as_codes() {
        assert_eq!(
            serde_json::to_value(ApiError::Network).unwrap(),
            json!("network")
        );
        assert_eq!(
            serde_json::to_value(ApiError::NotAllowed).unwrap(),
            json!("not_allowed")
        );
    }

    #[test]
    fn http_client_builds_with_public_roots_only() {
        assert!(http_client().is_ok());
        assert!(!webpki_root_certs::TLS_SERVER_ROOT_CERTS.is_empty());
    }
}
