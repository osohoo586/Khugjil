//! Build-time configuration. The server host is compiled in (PLAN-PHASE2 §1, decision 3): a pairing
//! code can never point the agent at another server.

/// Cashier API host. Override at build time with `KHJ_AGENT_SERVER=app.example.test cargo build`.
pub const SERVER_HOST: &str = match option_env!("KHJ_AGENT_SERVER") {
    Some(host) => host,
    None => "app.khugjil.usukh.shop",
};

/// Sent as `X-Khj-Agent`; the server refuses versions below its minimum with HTTP 426.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Service name of the Windows Credential Manager entry that holds the device token.
pub const CREDENTIAL_SERVICE: &str = "khugjil-agent";

pub fn base_url() -> String {
    format!("https://{SERVER_HOST}")
}

/// A host name only: lowercase letters, digits, dots and hyphens; no scheme, port, path or user.
pub fn valid_host(host: &str) -> bool {
    !host.is_empty()
        && host.len() <= 253
        && !host.starts_with(['.', '-'])
        && !host.ends_with(['.', '-'])
        && !host.contains("..")
        && host
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.' || b == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compiled_in_host_is_a_plain_host_name() {
        assert!(valid_host(SERVER_HOST));
        assert_eq!(base_url(), format!("https://{SERVER_HOST}"));
    }

    #[test]
    fn rejects_anything_that_is_not_a_host() {
        for bad in [
            "",
            "https://app.example",
            "app.example:8443",
            "app.example/x",
            "user@app.example",
            "APP.example",
            ".app.example",
            "app..example",
            "app.example.",
        ] {
            assert!(!valid_host(bad), "{bad}");
        }
    }

    #[test]
    fn version_is_strict_semver() {
        let parts: Vec<&str> = VERSION.split('.').collect();
        assert_eq!(parts.len(), 3);
        assert!(parts.iter().all(|p| p.parse::<u32>().is_ok()));
    }
}
