//! Khugjil desktop agent (PLAN-PHASE2.md): a tray program that runs the cashier view and shows
//! each incoming transaction in an always-on-top overlay that never takes focus from the POS.

pub mod api;
pub mod config;
pub mod creds;
pub mod overlay;
pub mod routes;
pub mod sse;
pub mod stream;
#[cfg(windows)]
pub mod win;

use std::sync::{Arc, Mutex};

use serde::Serialize;
use serde_json::Value;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::webview::NewWindowResponse;
use tauri::{
    AppHandle, Manager, State, Url, UserAttentionType, WebviewUrl, WebviewWindowBuilder, Window,
    WindowEvent, Wry,
};
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_updater::UpdaterExt as _;

use crate::api::{ApiClient, ApiError, IpcResponse};
use crate::creds::CredentialStore;
use crate::overlay::{Note, OverlayState};
use crate::stream::{StreamControl, StreamObserver};

const MAIN: &str = "main";
/// WebView2 flags. Every webview of the app must use identical flags (they share one browser
/// process), so both windows use this. Keeps wry's defaults and allows the chime without a click.
#[cfg(windows)]
const BROWSER_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
                            --autoplay-policy=no-user-gesture-required";

struct AppState {
    api: Arc<ApiClient>,
    stream: StreamControl,
    overlay: Mutex<OverlayState>,
    observer: Arc<Observer>,
}

/// Tray status text and the updater trigger, driven by the stream task.
struct Observer {
    app: AppHandle,
    status: Mutex<Option<MenuItem<Wry>>>,
}

impl StreamObserver for Observer {
    fn connected(&self, ok: bool) {
        let (mark, label) = if ok {
            ("●", "Холбогдсон")
        } else {
            ("○", "Холболт тасарсан")
        };
        if let Ok(slot) = self.status.lock() {
            if let Some(item) = slot.as_ref() {
                let _ = item.set_text(format!("{mark} {label}"));
            }
        }
        if let Some(tray) = self.app.tray_by_id("main") {
            let _ = tray.set_tooltip(Some(format!("Хугжил — {label}")));
        }
    }

    fn update_required(&self) {
        let app = self.app.clone();
        tauri::async_runtime::spawn(async move { install_update(app).await });
    }
}

#[derive(Debug, thiserror::Error)]
enum CmdError {
    #[error("not allowed")]
    NotAllowed,
    #[error("invalid")]
    Invalid,
    #[error(transparent)]
    Api(#[from] ApiError),
}

impl Serialize for CmdError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::NotAllowed => s.serialize_str("not_allowed"),
            Self::Invalid => s.serialize_str("invalid"),
            Self::Api(e) => e.serialize(s),
        }
    }
}

/// Capabilities already restrict commands per window; this is a second, independent check.
fn require(window: &Window, label: &str) -> Result<(), CmdError> {
    #[cfg(debug_assertions)]
    eprintln!("[khj] command from {}", window.label());
    if window.label() == label {
        Ok(())
    } else {
        Err(CmdError::NotAllowed)
    }
}

// ---------------------------------------------------------------- commands (main window)

#[tauri::command]
async fn api_request(
    window: Window,
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<IpcResponse, CmdError> {
    require(&window, MAIN)?;
    #[cfg(debug_assertions)]
    eprintln!(
        "[khj] api_request {method} {}",
        path.split('?').next().unwrap_or("")
    );
    let pairing = path == "/api/v1/pair";
    let response = state.api.request(&method, &path, body).await?;
    if pairing && response.status == 200 {
        // A newly paired cashier PC starts the agent at Windows sign-in (tray option to turn off).
        let _ = window.app_handle().autolaunch().enable();
    }
    Ok(response)
}

#[tauri::command]
fn stream_start(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    let observer: Arc<dyn StreamObserver> = state.observer.clone();
    state.stream.start(app, state.api.clone(), observer);
    Ok(())
}

#[tauri::command]
fn stream_stop(window: Window, state: State<'_, AppState>) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    state.stream.stop();
    Ok(())
}

#[tauri::command]
fn overlay_show(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
    note: Note,
) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    if !note.valid() {
        return Err(CmdError::Invalid);
    }
    let ready = state
        .overlay
        .lock()
        .map_err(|_| CmdError::Invalid)?
        .accept(note);
    if let Some(note) = ready {
        overlay::send(&app, &note);
    }
    Ok(())
}

#[tauri::command]
fn overlay_config(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
    seconds: u32,
) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    let send = state
        .overlay
        .lock()
        .map_err(|_| CmdError::Invalid)?
        .set_seconds(seconds);
    if let Some(seconds) = send {
        overlay::send_seconds(&app, seconds);
    }
    Ok(())
}

#[tauri::command]
fn overlay_clear(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    state
        .overlay
        .lock()
        .map_err(|_| CmdError::Invalid)?
        .clear_pending();
    overlay::send_clear(&app);
    Ok(())
}

#[tauri::command]
fn attention(window: Window, on: bool) -> Result<(), CmdError> {
    require(&window, MAIN)?;
    let kind = (on && !window.is_focused().unwrap_or(false)).then_some(UserAttentionType::Critical);
    let _ = window.request_user_attention(kind);
    Ok(())
}

// ---------------------------------------------------------------- commands (overlay window)

#[tauri::command]
fn overlay_ready(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), CmdError> {
    require(&window, overlay::LABEL)?;
    #[cfg(debug_assertions)]
    eprintln!("[khj] overlay_ready");
    let (seconds, pending) = state
        .overlay
        .lock()
        .map_err(|_| CmdError::Invalid)?
        .mark_ready();
    overlay::send_seconds(&app, seconds);
    for note in &pending {
        overlay::send(&app, note);
    }
    Ok(())
}

#[tauri::command]
fn overlay_layout(window: Window, app: AppHandle, height: f64) -> Result<(), CmdError> {
    require(&window, overlay::LABEL)?;
    if !height.is_finite() {
        return Err(CmdError::Invalid);
    }
    overlay::layout(&app, height).map_err(|_| CmdError::Invalid)
}

// ---------------------------------------------------------------- windows, tray, updates

/// Only the bundled pages may load; nothing navigates to the web or opens new windows.
pub fn local_url(url: &Url) -> bool {
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    }
}

fn window_builder<'a>(
    app: &'a tauri::App,
    label: &'a str,
    page: &str,
) -> WebviewWindowBuilder<'a, Wry, tauri::App> {
    let builder = WebviewWindowBuilder::new(app, label, WebviewUrl::App(page.into()))
        .on_navigation(local_url)
        .on_new_window(|_, _| NewWindowResponse::Deny);
    #[cfg(windows)]
    let builder = builder.additional_browser_args(BROWSER_ARGS);
    builder
}

fn create_windows(app: &tauri::App) -> tauri::Result<()> {
    window_builder(app, MAIN, "agent.html")
        .title("Орлого — Хугжил")
        .inner_size(1024.0, 720.0)
        .min_inner_size(360.0, 480.0)
        .build()?;
    let overlay = window_builder(app, overlay::LABEL, "overlay.html")
        .title("Орлого")
        .inner_size(overlay::WIDTH, 112.0)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .focusable(false)
        .focused(false)
        .visible(false)
        .build()?;
    #[cfg(windows)]
    {
        let hwnd = overlay.hwnd()?.0;
        // SAFETY: the handle comes from Tauri for the overlay window just created by this process.
        unsafe { win::make_overlay(hwnd) };
    }
    #[cfg(not(windows))]
    let _ = overlay;
    Ok(())
}

fn show_main(app: &AppHandle) {
    if let Some(main) = app.get_webview_window(MAIN) {
        let _ = main.unminimize();
        let _ = main.show();
        let _ = main.set_focus();
    }
}

fn quit(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        #[cfg(windows)]
        let yes = win::confirm(
            "Хугжил",
            "Програмаас гарвал орлогын мэдэгдэл ирэхгүй. Гарах уу?",
        );
        #[cfg(not(windows))]
        let yes = true;
        if yes {
            app.exit(0);
        }
    });
}

fn create_tray(app: &tauri::App, observer: &Observer) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", "○ Холбогдож байна…", false, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Харах", true, None::<&str>)?;
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        "Windows эхлэхэд асаах",
        true,
        autostart_on,
        None::<&str>,
    )?;
    let about = MenuItem::with_id(
        app,
        "about",
        format!("Хувилбар {} · {}", config::VERSION, config::SERVER_HOST),
        false,
        None::<&str>,
    )?;
    let quit_item = MenuItem::with_id(app, "quit", "Гарах", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &PredefinedMenuItem::separator(app)?,
            &show,
            &autostart,
            &about,
            &PredefinedMenuItem::separator(app)?,
            &quit_item,
        ],
    )?;
    if let Ok(mut slot) = observer.status.lock() {
        *slot = Some(status);
    }
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Хугжил")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "autostart" => {
                let manager = app.autolaunch();
                let on = manager.is_enabled().unwrap_or(false);
                let _ = if on {
                    manager.disable()
                } else {
                    manager.enable()
                };
                let _ = autostart.set_checked(manager.is_enabled().unwrap_or(false));
            }
            "quit" => quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let tauri::tray::TrayIconEvent::DoubleClick { .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

/// Check for a signed update and install it (start-up, and whenever the server answers 426).
/// Without a configured public key the updater is disabled and this does nothing.
async fn install_update(app: AppHandle) {
    let Ok(updater) = app.updater() else { return };
    if let Ok(Some(update)) = updater.check().await {
        if update.download_and_install(|_, _| {}, || {}).await.is_ok() {
            app.restart();
        }
    }
}

fn credential_store() -> Arc<dyn CredentialStore> {
    #[cfg(any(windows, target_os = "macos"))]
    if let Ok(store) = creds::KeyringStore::new(config::CREDENTIAL_SERVICE, config::SERVER_HOST) {
        return Arc::new(store);
    }
    Arc::new(creds::MemoryStore::default())
}

pub fn run() {
    assert!(
        config::valid_host(config::SERVER_HOST),
        "invalid KHJ_AGENT_SERVER"
    );
    let api = Arc::new(ApiClient::new(credential_store()).expect("HTTPS client"));
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app)
        }))
        .plugin(tauri_plugin_autostart::Builder::new().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            api_request,
            stream_start,
            stream_stop,
            overlay_show,
            overlay_config,
            overlay_clear,
            attention,
            overlay_ready,
            overlay_layout,
        ])
        .setup(move |app| {
            let observer = Arc::new(Observer {
                app: app.handle().clone(),
                status: Mutex::new(None),
            });
            create_windows(app)?;
            create_tray(app, &observer)?;
            app.manage(AppState {
                api,
                stream: StreamControl::default(),
                overlay: Mutex::new(OverlayState::default()),
                observer,
            });
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move { install_update(handle).await });
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the cashier window keeps the agent (and its notifications) running in the tray.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == MAIN {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the Khugjil agent");
}

/// Kept for tests: the main window label and the pages the agent loads.
pub fn pages() -> [(&'static str, &'static str); 2] {
    [(MAIN, "agent.html"), (overlay::LABEL, "overlay.html")]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_bundled_pages_may_load() {
        for ok in [
            "tauri://localhost/agent.html",
            "http://tauri.localhost/overlay.html",
            "https://tauri.localhost/",
        ] {
            assert!(local_url(&Url::parse(ok).unwrap()), "{ok}");
        }
        for bad in [
            "https://app.khugjil.usukh.shop/",
            "https://tauri.localhost.evil.example/",
            "http://localhost:1420/",
            "file:///C:/Windows/",
            "javascript:alert(1)",
            "data:text/html,hi",
            "tauri://evil/",
        ] {
            assert!(!local_url(&Url::parse(bad).unwrap()), "{bad}");
        }
    }

    #[test]
    fn pages_match_the_capability_windows() {
        assert_eq!(
            pages(),
            [("main", "agent.html"), ("overlay", "overlay.html")]
        );
    }
}
