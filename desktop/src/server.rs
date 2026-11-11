//! Find or start the Loft server.
//!
//! 1. A Loft already answering on 127.0.0.1:$LOFT_PORT (default 7717) is reused, so the CLI
//!    (`loft`, `loft app --browser`) and the app share one server. It is never stopped by us.
//! 2. Otherwise we run `python3 server.py --port <7717 or 0> --exit-with-stdin` from the
//!    bundled files (or $LOFT_REPO, a clone). The server prints `loft on http://127.0.0.1:N`
//!    once it listens; that line is the handshake. It exits when its stdin closes, so it goes
//!    away with the app even if the app is killed, and we also stop it on quit.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, Url};

const DEFAULT_PORT: u16 = 7717;

const APPIMAGE_ENV: &[&str] = &[
    "PYTHONHOME", "PYTHONPATH", "PERLLIB", "LD_LIBRARY_PATH", "LD_PRELOAD",
    "GST_PLUGIN_PATH", "GST_PLUGIN_PATH_1_0", "GST_PLUGIN_SYSTEM_PATH", "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_PLUGIN_SCANNER", "GST_PLUGIN_SCANNER_1_0", "GST_REGISTRY", "GST_REGISTRY_1_0",
    "GIO_MODULE_DIR", "GIO_EXTRA_MODULES", "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR",
    "GTK_PATH", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX", "GTK_IM_MODULE_FILE", "GSETTINGS_SCHEMA_DIR",
    "QT_PLUGIN_PATH",
];

#[derive(Default)]
pub struct Backend {
    child: Mutex<Option<Child>>,
}

impl Backend {
    pub fn connect(&self, app: &AppHandle) -> Result<Url, String> {
        let port = std::env::var("LOFT_PORT")
            .ok()
            .and_then(|p| p.parse::<u16>().ok())
            .unwrap_or(DEFAULT_PORT);
        if is_loft(port) {
            log(&format!("attaching to the Loft server on port {port}"));
            return Ok(url(port));
        }
        // the port is taken by something else: let the server pick a free one
        let bind = if port_open(port) { 0 } else { port };
        let child = self.spawn(app, bind, port)?;
        Ok(url(child))
    }

    fn spawn(&self, app: &AppHandle, bind: u16, preferred: u16) -> Result<u16, String> {
        let repo = repo_dir(app).ok_or("Couldn't find Loft's server files (server.py).")?;
        let path = child_path();
        let python = std::env::var("LOFT_PYTHON")
            .ok()
            .or_else(|| which("python3", &path))
            .ok_or("Python 3 wasn't found. Install Python 3.9+ and open Loft again.")?;
        let logf = log_path();
        let stderr = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&logf)
            .map(Stdio::from)
            .unwrap_or(Stdio::null());

        let mut cmd = Command::new(&python);
        cmd.arg("-u")
            .arg(repo.join("server.py"))
            .args(["--port", &bind.to_string(), "--exit-with-stdin"])
            .current_dir(&repo)
            .env("PATH", &path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(stderr);
        if std::env::var_os("APPIMAGE").is_some() {
            // AppRun points these at the AppImage's own libraries; the server, claude and
            // ffmpeg are host programs and must not see them
            for k in APPIMAGE_ENV {
                cmd.env_remove(k);
            }
        }
        if bind != DEFAULT_PORT || preferred != DEFAULT_PORT {
            // phone access belongs to the main server on 7717, not to a side instance
            cmd.arg("--no-tailnet");
        }
        log(&format!("starting {} {}/server.py --port {bind}", python, repo.display()));
        let mut child = cmd.spawn().map_err(|e| format!("Couldn't start the Loft server ({python}): {e}"))?;

        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = mpsc::channel::<u16>();
        std::thread::spawn(move || {
            let mut out = std::fs::OpenOptions::new().create(true).append(true).open(log_path()).ok();
            let mut sent = false;
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if !sent {
                    if let Some(p) = line.strip_prefix("loft on http://127.0.0.1:").and_then(|p| p.trim().parse().ok()) {
                        let _ = tx.send(p);
                        sent = true;
                    }
                }
                if let Some(f) = out.as_mut() {
                    let _ = writeln!(f, "{line}");
                }
            }
        });
        *self.child.lock().unwrap() = Some(child);

        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Ok(p) = rx.recv_timeout(Duration::from_millis(200)) {
                log(&format!("own server listening on port {p}"));
                return Ok(p);
            }
            let exited = self.child.lock().unwrap().as_mut().and_then(|c| c.try_wait().ok().flatten());
            if let Some(status) = exited {
                *self.child.lock().unwrap() = None;
                return Err(format!("The Loft server stopped ({status}). See {}", logf.display()));
            }
            if Instant::now() > deadline {
                self.shutdown();
                return Err(format!("The Loft server didn't start in time. See {}", logf.display()));
            }
        }
    }

    /// Stop the server only if this app started it.
    pub fn shutdown(&self) {
        let Some(mut child) = self.child.lock().unwrap().take() else { return };
        drop(child.stdin.take()); // --exit-with-stdin: the server exits on EOF
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = child.try_wait() {
                log("own server stopped");
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
        log("own server killed after timeout");
    }
}

fn url(port: u16) -> Url {
    Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap()
}

fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// The proof key the Loft server on `port` wrote to its private (0600) file, if any.
fn key_file(port: u16) -> Option<String> {
    let base = std::env::var("XDG_CACHE_HOME")
        .ok()
        .filter(|s| s.starts_with('/'))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".cache"));
    let f = base.join("loft").join(format!("server-{port}.key"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let m = f.metadata().ok()?;
        let me = std::fs::metadata(std::env::var("HOME").ok()?).ok()?.uid();
        // must be ours and private, or another user could plant a key
        if m.uid() != me || m.permissions().mode() & 0o077 != 0 {
            return None;
        }
    }
    let t = std::fs::read_to_string(f).ok()?.trim().to_string();
    (t.len() >= 32 && t.chars().all(|c| c.is_ascii_hexdigit())).then_some(t)
}

/// The server on the port is this user's live Loft: it answers a fresh random challenge with
/// sha256(key:nonce), which needs the key from the private file (never sent over the wire).
fn is_loft(port: u16) -> bool {
    use sha2::{Digest, Sha256};
    let Some(key) = key_file(port) else { return false };
    let Some(nonce) = nonce() else { return false };
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(3)));
    let req = format!("GET /api/loft-proof?n={nonce} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut body = Vec::new();
    let _ = s.take(64_000).read_to_end(&mut body);
    let text = String::from_utf8_lossy(&body);
    let want: String = Sha256::digest(format!("{key}:{nonce}").as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
    let ok = text.lines().next().is_some_and(|l| l.starts_with("HTTP/1.") && l.split(' ').nth(1) == Some("200"));
    ok && text.split("\r\n\r\n").nth(1).map(str::trim) == Some(want.as_str())
}

/// 16 random bytes as hex, from the OS.
fn nonce() -> Option<String> {
    let mut b = [0u8; 16];
    std::fs::File::open("/dev/urandom").ok()?.read_exact(&mut b).ok()?;
    Some(b.iter().map(|x| format!("{x:02x}")).collect())
}

/// The Loft files: $LOFT_REPO (a clone, for development) or the copy bundled with the app.
fn repo_dir(app: &AppHandle) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(r) = std::env::var("LOFT_REPO") {
        candidates.push(PathBuf::from(r));
    }
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.join("loft"));
    }
    candidates.into_iter().find(|d| d.join("server.py").is_file())
}

/// PATH for the server: the login shell's PATH (apps started from the dock or a .desktop
/// entry don't get the shell's), then ours, then the usual install dirs for claude/ffmpeg.
fn child_path() -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut dirs: Vec<String> = Vec::new();
    // inside an AppImage, AppRun puts the mounted image's usr/bin first: drop it
    let appdir = std::env::var("APPDIR").ok().filter(|d| !d.is_empty());
    let mut add = |s: &str| {
        for d in s.split(':') {
            if appdir.as_deref().is_some_and(|a| d.starts_with(a)) {
                continue;
            }
            if !d.is_empty() && !dirs.iter().any(|x| x == d) {
                dirs.push(d.to_string());
            }
        }
    };
    if let Some(p) = login_shell("printf %s \"$PATH\"") {
        add(&p);
    }
    add(&std::env::var("PATH").unwrap_or_default());
    add(&format!("{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"));
    dirs.join(":")
}

fn login_shell(script: &str) -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| {
        if cfg!(target_os = "macos") { "/bin/zsh".into() } else { "/bin/sh".into() }
    });
    let mut cmd = Command::new(shell);
    if std::env::var_os("APPIMAGE").is_some() {
        for k in APPIMAGE_ENV {
            cmd.env_remove(k);
        }
    }
    let mut child = cmd
        .args(["-lc", script])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + Duration::from_secs(4);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    let out = out.lines().last().unwrap_or("").trim().to_string();
    (!out.is_empty()).then_some(out)
}

fn which(name: &str, path: &str) -> Option<String> {
    path.split(':')
        .map(|d| Path::new(d).join(name))
        .find(|p| is_executable(p))
        .map(|p| p.to_string_lossy().into_owned())
}

fn is_executable(p: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        p.is_file()
    }
}

fn log_path() -> PathBuf {
    let base = std::env::var("XDG_CACHE_HOME")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let home = PathBuf::from(std::env::var("HOME").unwrap_or_default());
            if cfg!(target_os = "macos") { home.join("Library/Logs") } else { home.join(".cache") }
        })
        .join("loft");
    let _ = std::fs::create_dir_all(&base);
    base.join("desktop.log")
}

pub fn log(msg: &str) {
    eprintln!("loft: {msg}");
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(log_path()) {
        let _ = writeln!(f, "[desktop] {msg}");
    }
}
