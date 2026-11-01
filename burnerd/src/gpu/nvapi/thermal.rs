//! Optional NVIDIA hotspot telemetry. ABI facts were checked against LACT's
//! thermal-channel discovery and Blackwell register reader (commit 705cc8e):
//! https://github.com/ilya-zlobintsev/LACT/blob/705cc8e66706b05568cd49fe2f07bfe1476b7456/lact-daemon/src/server/gpu_controller/nvidia/nvapi.rs
//! This implementation shares PenguinBurner's existing telemetry session.

use std::mem::size_of;
use std::os::raw::c_int;

use nvml_wrapper::enums::device::DeviceArchitecture;

use super::{make_version, try_query_interface, NvapiHandle, QueryFn};

const THERMAL_INFO: u32 = 0x0BC8_163D;
const THERMAL_STATUS: u32 = 0x65FE_3AAD;
const REGISTER_QUERY: u32 = 0x2EB3_C140;
const BLACKWELL_HOTSPOT: u32 = 0x00AD_0AA0;
const READ_GLOBAL_U32: u16 = 1 | 4 | 16;

type InfoFn = unsafe extern "C" fn(NvapiHandle, *mut ThermalInfo) -> c_int;
type StatusFn = unsafe extern "C" fn(NvapiHandle, *mut ThermalStatus) -> c_int;
type RegisterFn = unsafe extern "C" fn(NvapiHandle, *mut RegisterQuery) -> c_int;

#[repr(C)]
struct ThermalInfo {
    version: u32,
    mask: u32,
    reserved: [u8; 32],
    // Each channel descriptor occupies 84 bytes. Only the driver's primary
    // channel mapping below is needed; do not infer a sensor from array order.
    channels: [[u8; 84]; 32],
    primary: [u8; 5],
}
const _: () = assert!(size_of::<ThermalInfo>() == 2736);

impl ThermalInfo {
    fn request() -> Self {
        Self {
            version: make_version(size_of::<Self>(), 2),
            mask: 0,
            reserved: [0; 32],
            channels: [[0; 84]; 32],
            primary: [u8::MAX; 5],
        }
    }

    fn hotspot_channel(&self) -> Option<u8> {
        let channel = self.primary[1]; // GPU hotspot is thermal target 1.
        (channel < 32 && self.mask & (1u32 << channel) != 0).then_some(channel)
    }
}

#[repr(C)]
struct ThermalStatus {
    version: u32,
    mask: u32,
    reserved: [u8; 32],
    temperatures: [i32; 32],
}
const _: () = assert!(size_of::<ThermalStatus>() == 168);

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct RegisterValue {
    flags: u16,
    status: u16,
    offset: u32,
    write_mask: u64,
    value: u64,
}
const _: () = assert!(size_of::<RegisterValue>() == 24);

#[repr(C)]
struct RegisterQuery {
    version: u32,
    count: u32,
    operations: [RegisterValue; 256],
}
const _: () = assert!(size_of::<RegisterQuery>() == 6152);

impl RegisterQuery {
    fn hotspot() -> Self {
        let mut request = Self {
            version: make_version(size_of::<Self>(), 1),
            count: 1,
            operations: [RegisterValue::default(); 256],
        };
        request.operations[0].flags = READ_GLOBAL_U32;
        request.operations[0].offset = BLACKWELL_HOTSPOT;
        request
    }
}

pub(super) struct HotspotReader {
    source: Source,
}

enum Source {
    Channel { query: StatusFn, index: u8 },
    Blackwell { query: RegisterFn },
}

impl HotspotReader {
    /// Caller retains the initialized library and GPU handle until drop.
    pub(super) unsafe fn open(
        query: QueryFn,
        gpu: NvapiHandle,
        architecture: Option<u32>,
    ) -> Option<Self> {
        let architecture = architecture?;
        let blackwell = DeviceArchitecture::Blackwell.as_c();
        let source = if architecture == blackwell {
            // SAFETY: this interface ID uses the RegisterQuery v1 ABI.
            let query = unsafe { try_query_interface::<RegisterFn>(query, REGISTER_QUERY) }?;
            Source::Blackwell { query }
        } else if architecture > 0 && architecture < blackwell {
            // SAFETY: these IDs use the thermal info/status v2 ABIs.
            let info = unsafe { try_query_interface::<InfoFn>(query, THERMAL_INFO) }?;
            // SAFETY: as above.
            let status = unsafe { try_query_interface::<StatusFn>(query, THERMAL_STATUS) }?;
            let mut request = ThermalInfo::request();
            // SAFETY: initialized GPU and correctly versioned output buffer.
            if unsafe { info(gpu, &mut request) } != 0 {
                return None;
            }
            Source::Channel {
                query: status,
                index: request.hotspot_channel()?,
            }
        } else {
            // Future architectures must not inherit an unverified register map.
            return None;
        };
        Some(Self { source })
    }

    /// Caller supplies the handle from the same live session used by `open`.
    pub(super) unsafe fn read(&self, gpu: NvapiHandle) -> Option<f64> {
        let raw = match self.source {
            Source::Channel { query, index } => {
                let mask = 1u32 << index;
                let mut request = ThermalStatus {
                    version: make_version(size_of::<ThermalStatus>(), 2),
                    mask,
                    reserved: [0; 32],
                    temperatures: [0; 32],
                };
                // SAFETY: index was validated at discovery; buffer matches v2.
                if unsafe { query(gpu, &mut request) } != 0 || request.mask & mask == 0 {
                    return None;
                }
                request.temperatures[usize::from(index)]
            }
            Source::Blackwell { query } => {
                // Exactly one fixed read. No caller-selected offset or write
                // opcode is exposed through this reader or the daemon API.
                let mut request = RegisterQuery::hotspot();
                // SAFETY: versioned v1 buffer, live handle, read-only operation.
                if unsafe { query(gpu, &mut request) } != 0
                    || request.count != 1
                    || request.operations[0].status != 0
                {
                    return None;
                }
                (request.operations[0].value & 0xffff) as i32
            }
        };
        decode_temperature(raw)
    }
}

fn decode_temperature(raw: i32) -> Option<f64> {
    let celsius = f64::from(raw) / 256.0;
    // Zero/unfilled slots and the 255 C sentinel must never look like readings.
    (celsius > 0.0 && celsius < 255.0).then_some(celsius)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uses_discovered_hotspot_not_a_fixed_channel() {
        let mut info = ThermalInfo::request();
        assert_eq!(info.hotspot_channel(), None);
        info.primary[1] = 31;
        assert_eq!(info.hotspot_channel(), None);
        info.mask = 1 << 31;
        assert_eq!(info.hotspot_channel(), Some(31));
        info.primary[1] = 32;
        assert_eq!(info.hotspot_channel(), None);
    }

    #[test]
    fn decodes_fractional_temperature_and_rejects_sentinels() {
        assert_eq!(decode_temperature(72 * 256 + 128), Some(72.5));
        for raw in [0, -256, 255 * 256, i32::MAX] {
            assert_eq!(decode_temperature(raw), None);
        }
    }

    unsafe extern "C" fn channel_query(_: NvapiHandle, output: *mut ThermalStatus) -> c_int {
        // SAFETY: tests pass the valid output buffer allocated by `read`.
        let output = unsafe { &mut *output };
        assert_eq!(output.version, 0x200a8);
        assert_eq!(output.mask, 1 << 7);
        output.temperatures[1] = 50 * 256;
        output.temperatures[7] = 80 * 256;
        0
    }

    unsafe extern "C" fn register_query(_: NvapiHandle, output: *mut RegisterQuery) -> c_int {
        // SAFETY: tests pass the valid output buffer allocated by `read`.
        let output = unsafe { &mut *output };
        assert_eq!(output.version, 0x11808);
        assert_eq!(output.count, 1);
        assert_eq!(output.operations[0].flags, READ_GLOBAL_U32);
        assert_eq!(output.operations[0].offset, BLACKWELL_HOTSPOT);
        assert!(output.operations.iter().all(|op| op.write_mask == 0));
        output.operations[0].value = 0x4000_5080;
        0
    }

    unsafe extern "C" fn register_failure(gpu: NvapiHandle, output: *mut RegisterQuery) -> c_int {
        // SAFETY: forwarded test buffer; simulate a failed individual operation.
        unsafe {
            register_query(gpu, output);
            (*output).operations[0].status = 1;
        }
        0
    }

    unsafe extern "C" fn channel_failure(_: NvapiHandle, _: *mut ThermalStatus) -> c_int {
        -1
    }

    unsafe extern "C" fn register_api_failure(_: NvapiHandle, _: *mut RegisterQuery) -> c_int {
        -1
    }

    unsafe extern "C" fn thermal_info(_: NvapiHandle, output: *mut ThermalInfo) -> c_int {
        // SAFETY: test discovery passes its valid versioned buffer.
        let output = unsafe { &mut *output };
        assert_eq!(output.version, 0x20ab0);
        output.mask = 1 << 7;
        output.primary[1] = 7;
        0
    }

    unsafe extern "C" fn test_interface(id: u32) -> *mut std::ffi::c_void {
        match id {
            THERMAL_INFO => thermal_info as *mut std::ffi::c_void,
            THERMAL_STATUS => channel_query as *mut std::ffi::c_void,
            REGISTER_QUERY => register_query as *mut std::ffi::c_void,
            _ => std::ptr::null_mut(),
        }
    }

    unsafe extern "C" fn missing_interface(_: u32) -> *mut std::ffi::c_void {
        std::ptr::null_mut()
    }

    #[test]
    fn selects_architecture_and_handles_missing_interfaces() {
        let gpu = std::ptr::null_mut();
        for (architecture, expected) in [
            (Some(DeviceArchitecture::Ada.as_c()), Some(80.0)),
            (Some(DeviceArchitecture::Blackwell.as_c()), Some(80.5)),
            (Some(DeviceArchitecture::Blackwell.as_c() + 1), None),
            (Some(DeviceArchitecture::Unknown.as_c()), None),
            (None, None),
        ] {
            // SAFETY: these test functions only access their output buffers.
            let value = unsafe {
                HotspotReader::open(test_interface, gpu, architecture)
                    .and_then(|reader| reader.read(gpu))
            };
            assert_eq!(value, expected);
            // SAFETY: the resolver supplies no callable pointers.
            assert!(unsafe { HotspotReader::open(missing_interface, gpu, architecture) }.is_none());
        }
    }

    #[test]
    fn reads_channel_and_fixed_blackwell_register() {
        for (source, expected) in [
            (
                Source::Channel {
                    query: channel_query,
                    index: 7,
                },
                Some(80.0),
            ),
            (
                Source::Channel {
                    query: channel_failure,
                    index: 7,
                },
                None,
            ),
            (
                Source::Blackwell {
                    query: register_query,
                },
                Some(80.5),
            ),
            (
                Source::Blackwell {
                    query: register_failure,
                },
                None,
            ),
            (
                Source::Blackwell {
                    query: register_api_failure,
                },
                None,
            ),
        ] {
            // SAFETY: test functions only access their provided output buffers.
            let value = unsafe { HotspotReader { source }.read(std::ptr::null_mut()) };
            assert_eq!(value, expected);
        }
    }
}
