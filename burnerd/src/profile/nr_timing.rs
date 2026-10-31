//! GPU evaluation durations, independent of frame latency and present pacing.
use std::collections::VecDeque;

use serde_json::{Map, Value};

const MAX_SAMPLES: usize = 1024;
const MAX_AGE_S: f64 = 3.0;

pub(super) struct NrSample {
    producer: String,
    serial: u64,
    gpu_us: f64,
}

impl NrSample {
    pub(super) fn parse(data: &Map<String, Value>) -> Option<Self> {
        if data.get("v")?.as_u64()? != 1
            || data.get("scope")?.as_str()? != "model"
            || data.get("source")?.as_str()? != "ngx-runtime"
            || data.get("session_id")?.as_str()?.is_empty()
        {
            return None;
        }
        let api = data.get("api")?.as_str()?;
        if !matches!(api, "d3d12" | "vulkan") {
            return None;
        }
        let pid = data.get("pid")?.as_u64()?;
        let device = data.get("device")?.as_str()?;
        let serial = data.get("sample_id")?.as_u64()?;
        let gpu_us = data.get("nr_gpu_us")?.as_f64()?;
        if pid == 0
            || serial == 0
            || device.is_empty()
            || device.len() > 64
            || !gpu_us.is_finite()
            || !(0.0..1_000_000.0).contains(&gpu_us)
            || gpu_us == 0.0
        {
            return None;
        }
        Some(Self {
            producer: format!("{pid}/{device}/{api}"),
            serial,
            gpu_us,
        })
    }
}

#[derive(Default)]
pub(super) struct NrMeter {
    samples: VecDeque<(NrSample, f64)>,
}

impl NrMeter {
    pub(super) fn add(&mut self, sample: NrSample, received: f64) {
        // Duplicate/reordered results cannot revive stale readings. Keep the
        // bounded history until the parent session is reset.
        if self
            .samples
            .iter()
            .rev()
            .any(|(old, _)| old.producer == sample.producer && old.serial >= sample.serial)
        {
            return;
        }
        if self.samples.len() == MAX_SAMPLES {
            self.samples.pop_front();
        }
        self.samples.push_back((sample, received));
    }

    pub(super) fn average_ms(&self, now: f64) -> Option<f64> {
        let (latest, time) = self.samples.back()?;
        if now - time > MAX_AGE_S {
            return None;
        }
        // Never average samples from different devices / processes as though
        // they were evaluations on one timeline. Use the latest NR producer.
        let mut sum = 0.0;
        let mut count = 0;
        for (sample, received) in &self.samples {
            if sample.producer == latest.producer && now - received <= MAX_AGE_S {
                sum += sample.gpu_us;
                count += 1;
            }
        }
        (count > 0).then(|| sum / f64::from(count) / 1000.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample(serial: u64, gpu_us: f64) -> NrSample {
        NrSample {
            producer: "game/device/d3d12".into(),
            serial,
            gpu_us,
        }
    }
    #[test]
    fn averages_evaluations_expires_and_rejects_duplicate_revival() {
        let mut meter = NrMeter::default();
        meter.add(sample(1, 2000.0), 10.0);
        meter.add(sample(2, 4000.0), 10.1);
        assert_eq!(meter.average_ms(11.0), Some(3.0));
        meter.add(sample(2, 9000.0), 14.0);
        assert_eq!(meter.average_ms(14.0), None);
    }
    #[test]
    fn keeps_devices_and_invalid_measurements_separate() {
        let mut meter = NrMeter::default();
        meter.add(sample(1, 2000.0), 1.0);
        let mut other = sample(2, 6000.0);
        other.producer = "helper/second-device/vulkan".into();
        meter.add(other, 1.1);
        assert_eq!(meter.average_ms(2.0), Some(6.0));
        let mut data = serde_json::json!({"v":1,"scope":"model","source":"ngx-runtime",
            "session_id":"123","api":"d3d12","pid":4,"device":"1",
            "sample_id":1,"nr_gpu_us":2200.5})
        .as_object()
        .unwrap()
        .clone();
        assert!(NrSample::parse(&data).is_some());
        for invalid in [
            serde_json::json!(-1),
            serde_json::json!(1000000),
            serde_json::Value::Null,
        ] {
            data.insert("nr_gpu_us".into(), invalid);
            assert!(NrSample::parse(&data).is_none());
        }
    }
}
