// Application commands are declared here so that capabilities (capabilities/*.json) decide which
// window may call which command. A command not granted to a window is rejected by Tauri.
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "api_request",
            "stream_start",
            "stream_stop",
            "overlay_show",
            "overlay_config",
            "overlay_clear",
            "overlay_ready",
            "overlay_layout",
            "attention",
        ]),
    ))
    .expect("failed to run tauri-build");
}
