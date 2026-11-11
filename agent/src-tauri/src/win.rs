//! The few Win32 calls Tauri does not cover (PLAN-PHASE2 §1.1): a non-activating, topmost tool
//! window for the overlay, and a native Yes/No box for quitting.
#![cfg(windows)]

use std::ffi::c_void;

use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetWindowLongPtrW, MessageBoxW, SetWindowLongPtrW, SetWindowPos, ShowWindow, GWL_EXSTYLE,
    HWND_TOPMOST, IDYES, MB_ICONWARNING, MB_SETFOREGROUND, MB_TOPMOST, MB_YESNO, SWP_NOACTIVATE,
    SWP_NOMOVE, SWP_NOSIZE, SW_SHOWNOACTIVATE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
};

/// Clicking the overlay never activates it, and it has no taskbar button or Alt+Tab entry.
///
/// # Safety
/// `hwnd` must be a live top-level window handle owned by this process (from Tauri).
pub unsafe fn make_overlay(hwnd: *mut c_void) {
    // SAFETY: guaranteed by the caller; only the extended style is changed.
    unsafe {
        let style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        let wanted = (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST) as isize;
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | wanted);
    }
}

/// Show (or keep showing) the overlay on top without taking focus from the POS program.
///
/// # Safety
/// `hwnd` must be a live top-level window handle owned by this process (from Tauri).
pub unsafe fn show_no_activate(hwnd: *mut c_void) {
    // SAFETY: guaranteed by the caller; both calls only change visibility and z-order.
    unsafe {
        ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        );
    }
}

/// A modal Yes/No question; returns true for Yes.
pub fn confirm(title: &str, text: &str) -> bool {
    let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    let (title, text) = (wide(title), wide(text));
    // SAFETY: both buffers are NUL-terminated UTF-16 and outlive the call.
    let answer = unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            title.as_ptr(),
            MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND,
        )
    };
    answer == IDYES
}
