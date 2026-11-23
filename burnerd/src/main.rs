mod api;
mod argvspec;
mod delete;
mod frame_history;
mod gpu;
mod gpu_rpc;
mod logging;
mod paths;
mod profile;
mod rtd3;
mod scan;
mod server;
mod sessions;
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
    let options = match parse_args(
        env::args().skip(1),
        env::var("PENGUIN_BURNERD_DEBUG").ok().as_deref(),
    ) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };
    let socket_path = options.socket;
    logging::set_debug_enabled(options.debug);

    logging::info(&format!(
        "penguin-burnerd {} ({}) starting",
        env!("CARGO_PKG_VERSION"),
        env!("PENGUIN_BURNERD_BUILD_ID"),
    ));
    if logging::debug_enabled() {
        logging::info("debug mode enabled: periodic telemetry and status logging active");
    }

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
    // `serve_daemon_api`, which runs autostart first). On RTD3 laptops the
    // deep-sleep gate defers this until the GPU is actually in use, so a
    // sleeping dGPU is never woken (and then pinned) just to apply a profile.
    rtd3::watch::startup(&sup);
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

#[derive(Debug)]
struct DaemonOptions {
    socket: PathBuf,
    debug: bool,
}

fn parse_args(
    mut args: impl Iterator<Item = String>,
    debug_env: Option<&str>,
) -> Result<DaemonOptions, String> {
    let mut socket = PathBuf::from(DEFAULT_SOCKET);
    let mut debug = debug_env.is_some_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    });
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "serve" => {}
            "--debug" => debug = true,
            "--socket" => {
                socket = PathBuf::from(args.next().ok_or("--socket requires a value")?);
            }
            other if other.starts_with("--socket=") => {
                socket = PathBuf::from(&other["--socket=".len()..]);
            }
            "--version" => {
                // Exit-0 identity probe. The Flatpak install transaction runs
                // this on the staged binary before committing, so a host whose
                // system libraries cannot load the daemon fails with the
                // loader's error instead of a broken service.
                println!(
                    "penguin-burnerd {} ({})",
                    env!("CARGO_PKG_VERSION"),
                    env!("PENGUIN_BURNERD_BUILD_ID"),
                );
                std::process::exit(0);
            }
            "-h" | "--help" => {
                return Err(
                    "usage: penguin-burnerd [serve] [--socket PATH] [--debug] [--version]\n\
                     --debug  Log periodic runtime telemetry and status (off by default).\n\
                     Also enabled by PENGUIN_BURNERD_DEBUG=1 (or true, yes, on)."
                        .to_string(),
                );
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(DaemonOptions { socket, debug })
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn periodic_logging_is_opt_in() {
        let options = parse_args(std::iter::empty(), None).unwrap();
        assert!(!options.debug);
        assert_eq!(options.socket, PathBuf::from(DEFAULT_SOCKET));
        for value in ["", "0", "false", "no", "off", "typo"] {
            assert!(!parse_args(std::iter::empty(), Some(value)).unwrap().debug);
        }
        for value in ["1", "true", "yes", "on", " TRUE "] {
            assert!(parse_args(std::iter::empty(), Some(value)).unwrap().debug);
        }
    }

    #[test]
    fn debug_flag_works_with_existing_daemon_arguments() {
        for socket_args in [
            vec!["--socket", "/tmp/test.sock"],
            vec!["--socket=/tmp/test.sock"],
        ] {
            let args = [vec!["serve", "--debug"], socket_args].concat();
            let options = parse_args(args.into_iter().map(str::to_string), Some("0")).unwrap();
            assert!(options.debug);
            assert_eq!(options.socket, PathBuf::from("/tmp/test.sock"));
        }
        assert!(parse_args(["--debgu".to_string()].into_iter(), None).is_err());
        assert!(parse_args(["--socket".to_string()].into_iter(), None).is_err());
        let help = parse_args(["--help".to_string()].into_iter(), None).unwrap_err();
        assert!(help.contains("--debug"));
        assert!(help.contains("PENGUIN_BURNERD_DEBUG"));
    }
}
