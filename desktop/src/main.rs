// Loft desktop: a native window around the Loft web UI.
//
// On launch it attaches to a Loft server that is already running on 127.0.0.1:$LOFT_PORT
// (default 7717, so the CLI and the app share one server), or starts its own from the
// bundled server files and stops it again on quit. The UI itself is unchanged: the page the
// server sends carries its per-run token, so the window only needs the URL.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod server;

use std::sync::Mutex;

use tauri::menu::{MenuBuilder, MenuItem, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{NewWindowResponse, PageLoadEvent, PermissionKind, PermissionResponse};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_window_state::StateFlags;

const MAIN: &str = "main";

/// Injected into every page. Loft's recorder asks for webm/opus, which WebKitGTK and WKWebView
/// may not record; fall back to the closest type they do (the server decodes anything with
/// ffmpeg, it sniffs the container rather than trusting the extension).
const INIT_JS: &str = r#"
(() => {
  const MR = window.MediaRecorder;
  if (!MR || MR.__loft) return;
  const prefs = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4", "audio/mp4;codecs=mp4a.40.2"];
  class LoftRecorder extends MR {
    constructor(stream, opts) {
      opts = Object.assign({}, opts || {});
      if (opts.mimeType && !MR.isTypeSupported(opts.mimeType)) {
        const t = prefs.find(p => MR.isTypeSupported(p));
        if (t) opts.mimeType = t; else delete opts.mimeType;
      }
      super(stream, opts);
    }
  }
  LoftRecorder.__loft = true;
  window.MediaRecorder = LoftRecorder;
})();
"#;

/// Focus the window and press the UI's own `n` (New task) shortcut.
const NEW_TASK_JS: &str = r#"
(() => {
  const ae = document.activeElement;
  if (ae && ae.blur) ae.blur();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }));
})();
"#;

struct Tray {
    _icon: TrayIcon,
    needs: MenuItem<tauri::Wry>,
}

#[derive(Default)]
struct UiState {
    tray: Mutex<Option<Tray>>,
    loaded_once: Mutex<bool>,
}

fn is_local(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "data" | "blob" => true,
        "http" | "https" | "ws" => matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "tauri.localhost")),
        _ => false,
    }
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn set_needs(app: &AppHandle, n: usize) {
    let state = app.state::<UiState>();
    let guard = state.tray.lock().unwrap();
    if let Some(t) = guard.as_ref() {
        let label = match n {
            0 => "Nothing needs you".to_string(),
            1 => "1 agent needs you".to_string(),
            n => format!("{n} agents need you"),
        };
        let _ = t.needs.set_text(&label);
        let _ = t._icon.set_tooltip(Some(if n == 0 { "Loft".to_string() } else { format!("Loft: {label}") }));
        #[cfg(target_os = "macos")]
        let _ = t._icon.set_title(if n == 0 { None } else { Some(n.to_string()) });
    }
}

/// The UI sets document.title to "(n) loft" when n agents need you.
fn needs_from_title(title: &str) -> usize {
    title
        .strip_prefix('(')
        .and_then(|r| r.split(')').next())
        .and_then(|n| n.trim().parse().ok())
        .unwrap_or(0)
}

fn build_tray(app: &AppHandle) -> tauri::Result<Tray> {
    let open = MenuItemBuilder::with_id("open", "Open Loft").build(app)?;
    let new_task = MenuItemBuilder::with_id("new", "New task").build(app)?;
    let needs = MenuItemBuilder::with_id("needs", "Nothing needs you").enabled(false).build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Quit Loft").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&open, &new_task, &needs]).separator().item(&quit).build()?;
    let mut b = TrayIconBuilder::with_id("loft").tooltip("Loft").menu(&menu).show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon() {
        b = b.icon(icon.clone());
    }
    let icon = b
        .on_menu_event(|app, ev| match ev.id().as_ref() {
            "open" => show_main(app),
            "new" => {
                show_main(app);
                if let Some(w) = app.get_webview_window(MAIN) {
                    let _ = w.eval(NEW_TASK_JS);
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(Tray { _icon: icon, needs })
}

fn splash_error(w: &WebviewWindow, msg: &str) {
    let js = format!("window.loftError && window.loftError({})", serde_json::to_string(msg).unwrap());
    let _ = w.eval(&js);
}

#[cfg(target_os = "linux")]
fn enable_media(w: &WebviewWindow) {
    let _ = w.with_webview(|wv| {
        use webkit2gtk::{SettingsExt, WebViewExt};
        if let Some(s) = WebViewExt::settings(&wv.inner()) {
            s.set_enable_media_stream(true);
            s.set_enable_mediasource(true);
            s.set_media_playback_requires_user_gesture(false);
        }
    });
}

fn main() {
    let app = tauri::Builder::default()
        // must be first: a second launch just focuses the running window
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_main(app)))
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::all() & !StateFlags::VISIBLE)
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .manage(UiState::default())
        .manage(server::Backend::default())
        .on_permission_request(|webview, kind| {
            // microphone (voice) and notifications, only for the local Loft page
            let local = webview.url().map(|u| is_local(&u) && u.scheme() == "http").unwrap_or(false);
            match kind {
                PermissionKind::Microphone | PermissionKind::Notifications if local => PermissionResponse::Allow,
                _ => PermissionResponse::Deny,
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();
            let nav_handle = handle.clone();
            let popup_handle = handle.clone();
            let title_handle = handle.clone();
            let load_handle = handle.clone();
            let window = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
                .title("Loft")
                .inner_size(1280.0, 820.0)
                .min_inner_size(720.0, 480.0)
                .background_color(tauri::window::Color(15, 16, 18, 255))
                .initialization_script(INIT_JS)
                .on_navigation(move |url| {
                    if is_local(url) {
                        return true;
                    }
                    let _ = nav_handle.opener().open_url(url.as_str(), None::<&str>);
                    false
                })
                .on_new_window(move |url, _features| {
                    if !is_local(&url) || url.scheme() == "http" {
                        let _ = popup_handle.opener().open_url(url.as_str(), None::<&str>);
                    }
                    NewWindowResponse::Deny
                })
                .on_document_title_changed(move |w, title| {
                    if w.url().map(|u| u.scheme() == "http").unwrap_or(false) {
                        set_needs(&title_handle, needs_from_title(&title));
                        let _ = w.set_title("Loft");
                    }
                })
                .on_page_load(move |w, p| {
                    if p.event() != PageLoadEvent::Finished || p.url().scheme() != "http" {
                        return;
                    }
                    // debug hook for automated checks: LOFT_DESKTOP_EVAL=<file.js> runs once after the UI loads
                    let state = load_handle.state::<UiState>();
                    let mut once = state.loaded_once.lock().unwrap();
                    if !*once {
                        *once = true;
                        if let Ok(path) = std::env::var("LOFT_DESKTOP_EVAL") {
                            if let Ok(js) = std::fs::read_to_string(path) {
                                let _ = w.eval(&js);
                            }
                        }
                    }
                })
                .build()?;

            #[cfg(target_os = "linux")]
            enable_media(&window);

            match build_tray(&handle) {
                Ok(t) => *app.state::<UiState>().tray.lock().unwrap() = Some(t),
                Err(e) => eprintln!("loft: no tray icon ({e}); closing the window quits"),
            }

            // find or start the server off the main thread, then point the window at it
            std::thread::spawn(move || {
                let w = handle.get_webview_window(MAIN).unwrap();
                let backend = handle.state::<server::Backend>();
                match backend.connect(&handle) {
                    Ok(url) => {
                        let _ = w.navigate(url);
                    }
                    Err(e) => splash_error(&w, &e),
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // with a tray icon, closing the window keeps Loft (and its notifications) running
                let has_tray = window.app_handle().state::<UiState>().tray.lock().unwrap().is_some();
                if has_tray && window.label() == MAIN {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Loft");

    app.run(|app, event| match event {
        RunEvent::Exit => app.state::<server::Backend>().shutdown(),
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(app),
        _ => {}
    });
}
