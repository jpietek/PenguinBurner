mod api;
mod argvspec;
mod delete;
mod frame_history;
mod gpu;
mod gpu_rpc;
mod logging;
mod paths;
mod profile;
mod scan;
mod server;
mod supervisor;

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use sd_notify::NotifyState;
use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;

use supervisor::Supervisor;

const DEFAULT_SOCKET: &str = "/run/penguin-burnerd.sock";
const WATCHDOG_INTERVAL: Duration = Duration::from_secs(10);

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    let socket_path = match parse_args() {
        Ok(path) => path,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };

    logging::info(&format!(
        "penguin-burnerd {} starting",
        env!("CARGO_PKG_VERSION")
    ));

    // Register before spawning the autostart engine so every termination
    // signal is routed to the cleanup thread.
    let signals = match Signals::new([SIGINT, SIGTERM]) {
        Ok(signals) => signals,
        Err(err) => {
            logging::error(&format!("failed to register termination signals: {err}"));
            return 1;
        }
    };

    let sup = Arc::new(Mutex::new(Supervisor::new()));

    // Start the persisted runtime profile before binding the socket (parity with
    // `serve_daemon_api`, which runs autostart first).
    supervisor::start_autostart_if_configured(&sup);
    supervisor::start_game_watch_monitor(&sup);

    // Route SIGINT/SIGTERM to a dedicated thread that performs a clean shutdown.
    {
        let sup = sup.clone();
        let socket_path = socket_path.clone();
        thread::Builder::new()
            .name("penguin-burner-signals".to_string())
            .spawn(move || wait_and_shutdown(signals, &sup, &socket_path))
            .expect("spawn signal thread");
    }

    let listener = match server::bind(&socket_path) {
        Ok(listener) => listener,
        Err(err) => {
            logging::error(&format!("{err}"));
            return 1;
        }
    };
    logging::info(&format!("listening on {}", socket_path.display()));

    let _ = sd_notify::notify(&[NotifyState::Ready]);
    thread::Builder::new()
        .name("penguin-burner-watchdog".to_string())
        .spawn(watchdog_loop)
        .expect("spawn watchdog thread");

    // Blocks forever; the process exits via the signal thread.
    server::serve(listener, sup);
    0
}

fn parse_args() -> Result<PathBuf, String> {
    let mut socket = PathBuf::from(DEFAULT_SOCKET);
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "serve" => {}
            "--socket" => {
                socket = PathBuf::from(args.next().ok_or("--socket requires a value")?);
            }
            other if other.starts_with("--socket=") => {
                socket = PathBuf::from(&other["--socket=".len()..]);
            }
            "-h" | "--help" => {
                return Err("usage: penguin-burnerd [serve] [--socket PATH]".to_string());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(socket)
}

// --- signal handling ---------------------------------------------------------

fn wait_and_shutdown(mut signals: Signals, sup: &Arc<Mutex<Supervisor>>, socket_path: &Path) -> ! {
    let received = signals.forever().next().unwrap_or(SIGTERM);
    logging::info(&format!("received signal {received}, shutting down"));
    let _ = sd_notify::notify(&[NotifyState::Stopping]);
    supervisor::shutdown(sup);
    let _ = fs::remove_file(socket_path);
    std::process::exit(0);
}

// --- systemd sd_notify + watchdog -------------------------------------------

fn watchdog_loop() {
    if env::var_os("NOTIFY_SOCKET").is_none() {
        return;
    }
    loop {
        thread::sleep(WATCHDOG_INTERVAL);
        // Runtime apply is a bounded synchronous transaction and may hold the
        // supervisor lock during slow driver readback. That is healthy work,
        // not a daemon hang. Engine initialization has its own timeout and the
        // supervisor retains a timed-out writer, so the process watchdog only
        // reports process/thread liveness here.
        let _ = sd_notify::notify(&[NotifyState::Watchdog]);
    }
}
