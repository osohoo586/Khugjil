//! The overlay window (PLAN-PHASE2 §1.1): always on top, never takes focus from the POS program,
//! bottom-right of the primary monitor, exactly as tall as its toast stack.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewWindow};

pub const LABEL: &str = "overlay";
pub const WIDTH: f64 = 360.0;
const MARGIN: f64 = 16.0;
const MAX_HEIGHT: f64 = 480.0;
const MAX_PENDING: usize = 10;

/// One note, as plain strings. Rust re-validates what the main window sends before forwarding it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Note {
    pub amount_text: String,
    pub description: String,
    pub time_text: String,
    pub account_alias: String,
    pub test: bool,
}

fn plain(s: &str, max_chars: usize) -> bool {
    s.chars().count() <= max_chars && !s.chars().any(char::is_control)
}

impl Note {
    /// The server already sanitizes descriptions (≤200 code points, no control or bidi
    /// characters); this is a second, independent bound.
    pub fn valid(&self) -> bool {
        plain(&self.amount_text, 40)
            && plain(&self.description, 200)
            && plain(&self.time_text, 20)
            && plain(&self.account_alias, 80)
    }
}

#[derive(Debug)]
pub struct OverlayState {
    ready: bool,
    pending: Vec<Note>,
    seconds: u32,
}

impl Default for OverlayState {
    fn default() -> Self {
        Self {
            ready: false,
            pending: Vec::new(),
            seconds: 8,
        }
    }
}

pub fn clamp_seconds(seconds: u32) -> u32 {
    seconds.clamp(5, 20)
}

impl OverlayState {
    /// Returns the note to send now, or keeps it until the overlay page has loaded.
    pub fn accept(&mut self, note: Note) -> Option<Note> {
        if self.ready {
            return Some(note);
        }
        if self.pending.len() == MAX_PENDING {
            self.pending.remove(0);
        }
        self.pending.push(note);
        None
    }

    pub fn set_seconds(&mut self, seconds: u32) -> Option<u32> {
        self.seconds = clamp_seconds(seconds);
        self.ready.then_some(self.seconds)
    }

    /// Called once the overlay page listens: returns the duration and the queued notes.
    pub fn mark_ready(&mut self) -> (u32, Vec<Note>) {
        self.ready = true;
        (self.seconds, std::mem::take(&mut self.pending))
    }

    pub fn clear_pending(&mut self) {
        self.pending.clear();
    }
}

pub fn send(app: &AppHandle, note: &Note) {
    let _ = app.emit_to(LABEL, "khj://overlay", note);
}

pub fn send_seconds(app: &AppHandle, seconds: u32) {
    let _ = app.emit_to(LABEL, "khj://overlay-seconds", seconds);
}

pub fn send_clear(app: &AppHandle) {
    let _ = app.emit_to(LABEL, "khj://overlay-clear", ());
}

/// Bottom-right corner of a work area, in physical pixels.
pub fn corner(
    area_pos: (i32, i32),
    area_size: (u32, u32),
    scale: f64,
    height: f64,
) -> (PhysicalPosition<i32>, PhysicalSize<u32>) {
    let w = (WIDTH * scale).round() as i32;
    let h = (height.clamp(1.0, MAX_HEIGHT) * scale).round() as i32;
    let m = (MARGIN * scale).round() as i32;
    let x = area_pos.0 + area_size.0 as i32 - w - m;
    let y = area_pos.1 + area_size.1 as i32 - h - m;
    (
        PhysicalPosition::new(x, y),
        PhysicalSize::new(w as u32, h as u32),
    )
}

/// Resize to `height` CSS pixels and show without activation, or hide when `height` is 0.
pub fn layout(app: &AppHandle, height: f64) -> tauri::Result<()> {
    let Some(win) = app.get_webview_window(LABEL) else {
        return Ok(());
    };
    if height.is_nan() || height <= 0.0 {
        return win.hide();
    }
    let monitor = match win.primary_monitor()? {
        Some(m) => Some(m),
        None => win.current_monitor()?,
    };
    let Some(monitor) = monitor else {
        return Ok(());
    };
    let area = monitor.work_area();
    let (pos, size) = corner(
        (area.position.x, area.position.y),
        (area.size.width, area.size.height),
        monitor.scale_factor(),
        height,
    );
    win.set_size(size)?;
    win.set_position(pos)?;
    show_without_activation(&win)
}

#[cfg(windows)]
fn show_without_activation(win: &WebviewWindow) -> tauri::Result<()> {
    let hwnd = win.hwnd()?.0;
    // SAFETY: the handle comes from Tauri for a window this process owns and keeps alive.
    unsafe { crate::win::show_no_activate(hwnd) };
    Ok(())
}

#[cfg(not(windows))]
fn show_without_activation(win: &WebviewWindow) -> tauri::Result<()> {
    win.show()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note() -> Note {
        Note {
            amount_text: "2,800 ₮".into(),
            description: "Худалдан авалт".into(),
            time_text: "08:37".into(),
            account_alias: "Төв дэлгүүр".into(),
            test: false,
        }
    }

    #[test]
    fn validates_notes() {
        assert!(note().valid());
        assert!(Note {
            description: "ж".repeat(200),
            ..note()
        }
        .valid());
        assert!(!Note {
            description: "ж".repeat(201),
            ..note()
        }
        .valid());
        assert!(!Note {
            description: "a\u{0007}b".into(),
            ..note()
        }
        .valid());
        assert!(!Note {
            amount_text: "1\n2".into(),
            ..note()
        }
        .valid());
        assert!(!Note {
            account_alias: "x".repeat(81),
            ..note()
        }
        .valid());
        let extra = r#"{"amount_text":"","description":"","time_text":"","account_alias":"","test":false,"html":"<b>"}"#;
        assert!(serde_json::from_str::<Note>(extra).is_err());
    }

    #[test]
    fn queues_until_ready_then_flushes_in_order() {
        let mut s = OverlayState::default();
        assert_eq!(s.set_seconds(99), None);
        for i in 0..12 {
            assert_eq!(
                s.accept(Note {
                    time_text: format!("{i}"),
                    ..note()
                }),
                None
            );
        }
        let (seconds, pending) = s.mark_ready();
        assert_eq!(seconds, 20);
        assert_eq!(pending.len(), MAX_PENDING);
        assert_eq!(pending[0].time_text, "2");
        assert_eq!(s.accept(note()), Some(note()));
        assert_eq!(s.set_seconds(1), Some(5));
    }

    #[test]
    fn sits_in_the_bottom_right_corner_of_the_work_area() {
        // 1920x1040 work area (taskbar at the bottom), 100 % scaling, one toast.
        let (pos, size) = corner((0, 0), (1920, 1040), 1.0, 112.0);
        assert_eq!((pos.x, pos.y), (1920 - 360 - 16, 1040 - 112 - 16));
        assert_eq!((size.width, size.height), (360, 112));
        // Second monitor to the left, 150 % scaling.
        let (pos, size) = corner((-2560, 0), (2560, 1380), 1.5, 112.0);
        assert_eq!((size.width, size.height), (540, 168));
        assert_eq!((pos.x, pos.y), (-2560 + 2560 - 540 - 24, 1380 - 168 - 24));
        // Height is capped.
        assert_eq!(corner((0, 0), (800, 600), 1.0, 5000.0).1.height, 480);
    }
}
