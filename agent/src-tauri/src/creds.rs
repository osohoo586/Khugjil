//! Where the device token lives (PLAN-PHASE2 §4): Windows Credential Manager (DPAPI, per Windows
//! user). Never in files, web storage, logs, URLs or IPC responses.

use std::sync::Mutex;
use zeroize::Zeroizing;

#[derive(Debug, thiserror::Error)]
#[error("credential store unavailable")]
pub struct StoreError;

pub trait CredentialStore: Send + Sync {
    fn get(&self) -> Result<Option<Zeroizing<String>>, StoreError>;
    fn set(&self, token: &str) -> Result<(), StoreError>;
    fn delete(&self) -> Result<(), StoreError>;
}

/// A device token as issued by the server: 32 random bytes, URL-safe base64 (43 characters).
pub fn valid_token(token: &str) -> bool {
    (32..=128).contains(&token.len())
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

#[cfg(any(windows, target_os = "macos"))]
pub struct KeyringStore {
    entry: keyring::Entry,
}

#[cfg(any(windows, target_os = "macos"))]
impl KeyringStore {
    pub fn new(service: &str, host: &str) -> Result<Self, StoreError> {
        keyring::Entry::new(service, host)
            .map(|entry| Self { entry })
            .map_err(|_| StoreError)
    }
}

#[cfg(any(windows, target_os = "macos"))]
impl CredentialStore for KeyringStore {
    fn get(&self) -> Result<Option<Zeroizing<String>>, StoreError> {
        match self.entry.get_password() {
            Ok(token) => Ok(Some(Zeroizing::new(token))),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(StoreError),
        }
    }

    fn set(&self, token: &str) -> Result<(), StoreError> {
        self.entry.set_password(token).map_err(|_| StoreError)
    }

    fn delete(&self) -> Result<(), StoreError> {
        match self.entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(StoreError),
        }
    }
}

/// In-memory store for tests and for platforms without a native store.
#[derive(Default)]
pub struct MemoryStore(Mutex<Option<Zeroizing<String>>>);

impl CredentialStore for MemoryStore {
    fn get(&self) -> Result<Option<Zeroizing<String>>, StoreError> {
        Ok(self.0.lock().map_err(|_| StoreError)?.clone())
    }

    fn set(&self, token: &str) -> Result<(), StoreError> {
        *self.0.lock().map_err(|_| StoreError)? = Some(Zeroizing::new(token.to_owned()));
        Ok(())
    }

    fn delete(&self) -> Result<(), StoreError> {
        *self.0.lock().map_err(|_| StoreError)? = None;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_shape() {
        assert!(valid_token(&"a".repeat(43)));
        assert!(valid_token("Ab-_0123456789abcdefghijklmnopqrstuvwxyz"));
        for bad in [
            "",
            "short",
            &"a".repeat(129),
            "has space aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "a/b+cAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ] {
            assert!(!valid_token(bad), "{bad}");
        }
    }

    #[test]
    fn memory_store_round_trip() {
        let store = MemoryStore::default();
        assert!(store.get().unwrap().is_none());
        store.set("token-1").unwrap();
        assert_eq!(
            store.get().unwrap().as_deref().map(String::as_str),
            Some("token-1")
        );
        store.delete().unwrap();
        assert!(store.get().unwrap().is_none());
    }
}
