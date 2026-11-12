// SceneDirector desktop shell.
//
// The window is a webview onto the UI in ../src. All the real work happens in
// the Python backend (directorc/server.py), which this process starts on a free
// local port and stops again when the window closes.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, RunEvent, State};

struct Backend {
    url: Result<String, String>,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn backend_url(backend: State<'_, Backend>) -> Result<String, String> {
    backend.url.clone()
}

fn is_project_root(dir: &Path) -> bool {
    dir.join("directorc").join("server.py").is_file()
}

/// The folder that holds `directorc/` and `projects/`.
/// Order: SCENEDIRECTOR_ROOT, then the nearest folder at or above the
/// executable. That covers both `SceneDirector.exe` placed in the checkout and
/// development builds under `ui/src-tauri/target/`.
fn project_root() -> Result<PathBuf, String> {
    if let Ok(dir) = std::env::var("SCENEDIRECTOR_ROOT") {
        let dir = PathBuf::from(dir);
        return if is_project_root(&dir) {
            Ok(dir)
        } else {
            Err(format!("SCENEDIRECTOR_ROOT ({}) has no directorc/server.py", dir.display()))
        };
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.ancestors().skip(1).find(|dir| is_project_root(dir)) {
            return Ok(dir.to_path_buf());
        }
    }
    Err("Could not find the SceneDirector folder (the one with cli.py). \
         Put SceneDirector.exe inside it, or set SCENEDIRECTOR_ROOT to it."
        .into())
}

/// SCENEDIRECTOR_PYTHON, then a project virtualenv, then `python` on PATH.
fn python_for(root: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("SCENEDIRECTOR_PYTHON") {
        return PathBuf::from(path);
    }
    let venv = if cfg!(windows) {
        ["Scripts", "python.exe"]
    } else {
        ["bin", "python"]
    };
    for name in [".venv", "venv"] {
        let candidate = root.join(name).join(venv[0]).join(venv[1]);
        if candidate.is_file() {
            return candidate;
        }
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

fn start_backend() -> Result<(String, Child), String> {
    let root = project_root()?;
    // `canonicalize` yields a \\?\ path on Windows, which Python dislikes as a cwd.
    let root = PathBuf::from(root.to_string_lossy().trim_start_matches(r"\\?\"));
    let python = python_for(&root);

    let port = TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr())
        .map_err(|e| format!("No free local port: {e}"))?
        .port();

    let mut command = Command::new(&python);
    command
        .args(["-m", "directorc.server", "--port", &port.to_string(), "--parent-watch"])
        .current_dir(&root)
        .env("PYTHONUNBUFFERED", "1")
        // The backend exits when this pipe closes, so it never outlives the app.
        .stdin(Stdio::piped());
    if cfg!(debug_assertions) {
        command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    } else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let child = command.spawn().map_err(|e| {
        format!("Could not start Python ({}): {e}. Install Python 3.10+ or set SCENEDIRECTOR_PYTHON.", python.display())
    })?;
    Ok((format!("http://127.0.0.1:{port}"), child))
}

fn main() {
    let (url, child) = match start_backend() {
        Ok((url, child)) => (Ok(url), Some(child)),
        Err(message) => (Err(message), None),
    };

    tauri::Builder::default()
        .manage(Backend { url, child: Mutex::new(child) })
        .invoke_handler(tauri::generate_handler![backend_url])
        .build(tauri::generate_context!())
        .expect("failed to start SceneDirector")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<Backend>().child.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
