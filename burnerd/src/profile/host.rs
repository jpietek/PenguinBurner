//! Host facts the overlay shows beside the GPU's: system memory, CPU package
//! temperature and CPU package power.
//!
//! These live together because they share one shape -- read a procfs or sysfs
//! file, tolerate its absence -- rather than because they are the same subject.
//! Every reading is best-effort and returns `None` when the box cannot answer:
//! a machine without RAPL, or with its CPU temperature behind a driver we do
//! not recognise, makes the overlay omit that row. A missing row is honest; a
//! zero would read as a real measurement.
//!
//! RAPL's energy counter is root-only on most kernels, which is one of the
//! reasons this belongs in the daemon rather than the desktop app.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// System memory in whole mebibytes, matching the units the GPU's VRAM is
/// published in so the overlay formats both the same way.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostMemory {
    pub used_mb: i64,
    pub total_mb: i64,
}

/// Parse `/proc/meminfo`.
///
/// "Used" is total minus *available*, not total minus free: Linux lends nearly
/// all idle memory to the page cache, so total-minus-free reports a nearly full
/// machine at all times and tells the reader nothing.
pub fn parse_meminfo(text: &str) -> Option<HostMemory> {
    let mut total_kb: Option<u64> = None;
    let mut available_kb: Option<u64> = None;
    for line in text.lines() {
        let Some((key, rest)) = line.split_once(':') else {
            continue;
        };
        let value = rest
            .split_whitespace()
            .next()
            .and_then(|v| v.parse::<u64>().ok());
        match key.trim() {
            "MemTotal" => total_kb = value,
            "MemAvailable" => available_kb = value,
            _ => {}
        }
    }
    let total_kb = total_kb?;
    let available_kb = available_kb?;
    if total_kb == 0 {
        return None;
    }
    let used_kb = total_kb.saturating_sub(available_kb);
    Some(HostMemory {
        used_mb: kib_to_mib(used_kb),
        total_mb: kib_to_mib(total_kb),
    })
}

pub fn read_memory(meminfo_path: &Path) -> Option<HostMemory> {
    parse_meminfo(&fs::read_to_string(meminfo_path).ok()?)
}

fn kib_to_mib(kib: u64) -> i64 {
    ((kib + 512) / 1024) as i64
}

/// The hwmon drivers that report a CPU package temperature, most specific
/// first. `zenpower` replaces `k10temp` on some AMD setups and reports the same
/// sensor under the same label.
const CPU_HWMON_DRIVERS: [&str; 3] = ["coretemp", "k10temp", "zenpower"];

/// Labels that name the whole-package sensor, most trustworthy first. Intel's
/// `coretemp` exposes one input per core plus "Package id 0"; reading a single
/// core instead would make the number jump with whichever core is busy.
/// AMD's `k10temp` adds "Tdie" only on parts whose "Tctl" carries a fixed
/// offset (Zen1/Zen+: +10 C on a 2700X, +20 C on an 1800X, +27 C on a
/// Threadripper 1950X), so "Tdie" is the real die temperature whenever it
/// exists and must win over "Tctl".
const PACKAGE_LABELS: [&str; 4] = ["Package id 0", "Tdie", "Tctl", "Tccd1"];

/// CPU package temperature in degrees Celsius.
///
/// Walks `hwmon_root` for a driver we recognise, then prefers the input whose
/// label names the package. Falls back to `temp1_input`, which is the package
/// on every driver listed above.
pub fn read_cpu_temperature_c(hwmon_root: &Path) -> Option<f64> {
    let hwmon = find_cpu_hwmon(hwmon_root)?;
    if let Some(path) = labelled_temp_input(&hwmon) {
        if let Some(value) = read_millidegrees(&path) {
            return Some(value);
        }
    }
    read_millidegrees(&hwmon.join("temp1_input"))
}

fn find_cpu_hwmon(hwmon_root: &Path) -> Option<PathBuf> {
    let mut entries: Vec<PathBuf> = fs::read_dir(hwmon_root)
        .ok()?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .collect();
    // Deterministic order: hwmon numbering is not stable across boots, so
    // without sorting the choice between two candidate drivers would not be
    // reproducible.
    entries.sort();
    for driver in CPU_HWMON_DRIVERS {
        for entry in &entries {
            let name = fs::read_to_string(entry.join("name")).unwrap_or_default();
            if name.trim() == driver {
                return Some(entry.clone());
            }
        }
    }
    None
}

fn labelled_temp_input(hwmon: &Path) -> Option<PathBuf> {
    let mut entries: Vec<PathBuf> = fs::read_dir(hwmon)
        .ok()?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("temp") && n.ends_with("_label"))
        })
        .collect();
    entries.sort();
    for wanted in PACKAGE_LABELS {
        for label_path in &entries {
            let label = fs::read_to_string(label_path).unwrap_or_default();
            if label.trim() == wanted {
                let input = label_path.to_string_lossy().replace("_label", "_input");
                return Some(PathBuf::from(input));
            }
        }
    }
    None
}

fn read_millidegrees(path: &Path) -> Option<f64> {
    let raw = fs::read_to_string(path).ok()?;
    let milli: i64 = raw.trim().parse().ok()?;
    // A sensor that has never been read can report 0; so can a genuinely cold
    // one, but a CPU at 0 C is not a case worth showing over omitting the row.
    if milli <= 0 {
        return None;
    }
    Some(milli as f64 / 1000.0)
}

/// One RAPL energy reading, so the sampler can be tested without a clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct EnergyReading {
    micro_joules: u64,
    range_uj: Option<u64>,
}

/// CPU package power, derived from RAPL's running energy counter.
///
/// Power is energy over time, so the first sample can only record a starting
/// point -- it returns `None`, and every sample after it reports the average
/// over the interval just elapsed.
#[derive(Debug, Default)]
pub struct CpuPowerSampler {
    last: Option<EnergyReading>,
}

impl CpuPowerSampler {
    pub fn new() -> Self {
        Self::default()
    }

    /// Average package watts since the previous sample, or `None` on the first
    /// call, on a box without RAPL, or when `elapsed` is zero.
    pub fn sample(&mut self, powercap_root: &Path, elapsed: Duration) -> Option<f64> {
        let reading = read_package_energy(powercap_root);
        let previous = self.last;
        self.last = reading;
        let (reading, previous) = (reading?, previous?);
        let seconds = elapsed.as_secs_f64();
        if seconds <= 0.0 {
            return None;
        }
        let delta_uj = if reading.micro_joules >= previous.micro_joules {
            reading.micro_joules - previous.micro_joules
        } else {
            // The counter wrapped. Without the declared range there is no way
            // to know by how much, so the sample is dropped rather than
            // guessed at.
            let range = reading.range_uj.or(previous.range_uj)?;
            range
                .checked_sub(previous.micro_joules)?
                .checked_add(reading.micro_joules)?
        };
        Some(delta_uj as f64 / 1_000_000.0 / seconds)
    }
}

/// The package-level RAPL domain. `intel-rapl` is the driver name on AMD too on
/// current kernels; the domain is identified by its own `name` file reading
/// `package-0` rather than by the directory it happens to sit in.
fn read_package_energy(powercap_root: &Path) -> Option<EnergyReading> {
    let mut entries: Vec<PathBuf> = fs::read_dir(powercap_root)
        .ok()?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .collect();
    entries.sort();
    for entry in entries {
        let name = fs::read_to_string(entry.join("name")).unwrap_or_default();
        if name.trim() != "package-0" {
            continue;
        }
        let micro_joules = fs::read_to_string(entry.join("energy_uj"))
            .ok()?
            .trim()
            .parse()
            .ok()?;
        let range_uj = fs::read_to_string(entry.join("max_energy_range_uj"))
            .ok()
            .and_then(|raw| raw.trim().parse().ok());
        return Some(EnergyReading {
            micro_joules,
            range_uj,
        });
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmpdir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "pb-host-{tag}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn meminfo_uses_available_not_free() {
        // 32 GiB total, 18 GiB available -> 14 GiB used. Total minus *free*
        // would have said 30 GiB, because the cache holds the rest.
        let text = "\
MemTotal:       33554432 kB
MemFree:         2097152 kB
MemAvailable:   18874368 kB
Buffers:          524288 kB
Cached:         12582912 kB
";
        let memory = parse_meminfo(text).unwrap();
        assert_eq!(memory.total_mb, 32768);
        assert_eq!(memory.used_mb, 14336);
    }

    #[test]
    fn meminfo_without_available_is_not_guessed() {
        // Kernels before 3.14 have no MemAvailable. Deriving it from free plus
        // cache is a guess, so the row is omitted instead.
        let text = "MemTotal:       33554432 kB\nMemFree:         2097152 kB\n";
        assert!(parse_meminfo(text).is_none());
    }

    #[test]
    fn meminfo_rejects_a_zero_total() {
        assert!(parse_meminfo("MemTotal: 0 kB\nMemAvailable: 0 kB\n").is_none());
        assert!(parse_meminfo("").is_none());
    }

    #[test]
    fn the_real_meminfo_parses() {
        // Every Linux box has this file and has had MemAvailable since 3.14,
        // so the shape is worth asserting against the real thing rather than
        // only against a fixture that agrees with the parser by construction.
        let memory = read_memory(Path::new("/proc/meminfo")).expect("/proc/meminfo");
        assert!(memory.total_mb > 0);
        assert!(memory.used_mb >= 0);
        assert!(memory.used_mb <= memory.total_mb);
    }

    #[test]
    fn the_real_sensors_never_panic() {
        // The result depends on the box, so there is nothing to assert about
        // the value -- only that walking a real sysfs tree is safe. A machine
        // with no recognised sensor answers None, which is the supported case.
        let _ = read_cpu_temperature_c(Path::new("/sys/class/hwmon"));
        let mut sampler = CpuPowerSampler::new();
        let _ = sampler.sample(Path::new("/sys/class/powercap"), Duration::from_secs(1));
    }

    #[test]
    fn cpu_temperature_prefers_the_package_sensor() {
        let root = tmpdir("temp");
        let hwmon = root.join("hwmon3");
        fs::create_dir_all(&hwmon).unwrap();
        fs::write(hwmon.join("name"), "coretemp\n").unwrap();
        // temp1 is the package; temp2 is one core running hotter.
        fs::write(hwmon.join("temp1_label"), "Package id 0\n").unwrap();
        fs::write(hwmon.join("temp1_input"), "54000\n").unwrap();
        fs::write(hwmon.join("temp2_label"), "Core 0\n").unwrap();
        fs::write(hwmon.join("temp2_input"), "71000\n").unwrap();

        assert_eq!(read_cpu_temperature_c(&root), Some(54.0));
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn cpu_temperature_prefers_tdie_over_an_offset_tctl() {
        let root = tmpdir("tdie");
        let hwmon = root.join("hwmon2");
        fs::create_dir_all(&hwmon).unwrap();
        fs::write(hwmon.join("name"), "k10temp\n").unwrap();
        // A 2700X: Tctl reads 10 C above the die.
        fs::write(hwmon.join("temp1_label"), "Tctl\n").unwrap();
        fs::write(hwmon.join("temp1_input"), "60000\n").unwrap();
        fs::write(hwmon.join("temp2_label"), "Tdie\n").unwrap();
        fs::write(hwmon.join("temp2_input"), "50000\n").unwrap();

        assert_eq!(read_cpu_temperature_c(&root), Some(50.0));
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn cpu_temperature_falls_back_to_the_first_input() {
        let root = tmpdir("fallback");
        let hwmon = root.join("hwmon1");
        fs::create_dir_all(&hwmon).unwrap();
        fs::write(hwmon.join("name"), "k10temp\n").unwrap();
        fs::write(hwmon.join("temp1_input"), "48500\n").unwrap();

        assert_eq!(read_cpu_temperature_c(&root), Some(48.5));
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn an_unknown_sensor_reports_nothing() {
        let root = tmpdir("unknown");
        let hwmon = root.join("hwmon0");
        fs::create_dir_all(&hwmon).unwrap();
        fs::write(hwmon.join("name"), "nvme\n").unwrap();
        fs::write(hwmon.join("temp1_input"), "40000\n").unwrap();

        assert_eq!(read_cpu_temperature_c(&root), None);
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_missing_hwmon_tree_reports_nothing() {
        assert_eq!(
            read_cpu_temperature_c(Path::new("/nonexistent-hwmon")),
            None
        );
    }

    fn write_rapl(root: &Path, micro_joules: u64, range: u64) {
        let domain = root.join("intel-rapl:0");
        fs::create_dir_all(&domain).unwrap();
        fs::write(domain.join("name"), "package-0\n").unwrap();
        fs::write(domain.join("energy_uj"), format!("{micro_joules}\n")).unwrap();
        fs::write(domain.join("max_energy_range_uj"), format!("{range}\n")).unwrap();
    }

    #[test]
    fn the_first_power_sample_only_starts_the_clock() {
        let root = tmpdir("rapl-first");
        write_rapl(&root, 1_000_000, 262_143_328_850);
        let mut sampler = CpuPowerSampler::new();

        assert_eq!(sampler.sample(&root, Duration::from_secs(1)), None);
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn power_is_energy_over_the_interval() {
        let root = tmpdir("rapl-delta");
        write_rapl(&root, 1_000_000, 262_143_328_850);
        let mut sampler = CpuPowerSampler::new();
        sampler.sample(&root, Duration::from_secs(1));

        // 45 joules more, two seconds later: 22.5 W.
        write_rapl(&root, 46_000_000, 262_143_328_850);
        let watts = sampler.sample(&root, Duration::from_secs(2)).unwrap();

        assert!((watts - 22.5).abs() < 1e-9, "got {watts}");
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_wrapped_counter_uses_the_declared_range() {
        let root = tmpdir("rapl-wrap");
        let range = 1_000_000_000u64;
        write_rapl(&root, range - 1_000_000, range);
        let mut sampler = CpuPowerSampler::new();
        sampler.sample(&root, Duration::from_secs(1));

        // Wraps past the top: 1 J before the end plus 2 J after it = 3 J.
        write_rapl(&root, 2_000_000, range);
        let watts = sampler.sample(&root, Duration::from_secs(1)).unwrap();

        assert!((watts - 3.0).abs() < 1e-9, "got {watts}");
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_box_without_rapl_reports_nothing() {
        let mut sampler = CpuPowerSampler::new();
        assert_eq!(
            sampler.sample(Path::new("/nonexistent-powercap"), Duration::from_secs(1)),
            None
        );
    }

    #[test]
    fn a_zero_interval_reports_nothing() {
        let root = tmpdir("rapl-zero");
        write_rapl(&root, 1_000_000, 262_143_328_850);
        let mut sampler = CpuPowerSampler::new();
        sampler.sample(&root, Duration::from_secs(1));
        write_rapl(&root, 2_000_000, 262_143_328_850);

        assert_eq!(sampler.sample(&root, Duration::ZERO), None);
        fs::remove_dir_all(&root).ok();
    }
}
