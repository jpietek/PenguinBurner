//! Hidden NVAPI (`libnvidia-api.so.1`) FFI for telemetry and per-point VF
//! curve control. Reached only through the single exported
//! `nvapi_QueryInterface(u32) -> *mut c_void`, casting each returned pointer to
//! a typed `extern "C"` fn. Every version-tagged `#[repr(C)]` struct carries a
//! compile-time size assertion — the driver rejects a wrong `version` (whose
//! low 16 bits are the struct byte size). Ports `hidden_nvapi_voltage.py`,
//! `hidden_nvapi_vf.py`, `hidden_nvapi_gpu_selection.py` (spec 03 §1–3).

use std::cell::{Cell, RefCell};
use std::ffi::c_void;
use std::mem::size_of;
use std::os::raw::{c_char, c_int};

use libloading::{Library, Symbol};

mod thermal;
use thermal::HotspotReader;

use super::{
    decode_cstr, vf_editable_core_points, vf_find_nearest, vf_summary_of, voltage_uv_in_window,
    GpuError, GpuResult, VfPoint, VfSummary,
};

type NvapiHandle = *mut c_void;

// Interface ids (shared lifecycle across both readers; distinct query fns).
const Q_INITIALIZE: u32 = 0x0150_E828;
const Q_UNLOAD: u32 = 0xD22B_DD7E;
const Q_GET_ERROR_MESSAGE: u32 = 0x6C2D_048C;
const Q_ENUM_PHYSICAL_GPUS: u32 = 0xE5AC_921F;
const Q_GET_BUS_ID: u32 = 0x1BE0_B8E5;
const Q_VOLTAGE: u32 = 0x465F_9BCF;
const Q_GET_VF_INFO: u32 = 0x507B_4B59;
const Q_GET_VF_STATUS: u32 = 0x2153_7AD4;
const Q_GET_VF_CONTROL: u32 = 0x23F1_B133;
const Q_SET_VF_CONTROL: u32 = 0x0733_E009;

const NVAPI_MAX_PHYSICAL_GPUS: usize = 64;
const NVAPI_SHORT_STRING_MAX: usize = 64;
const VF_POINTS_CAPACITY: usize = 255;

// --- FFI signatures --------------------------------------------------------
type QueryFn = unsafe extern "C" fn(u32) -> *mut c_void;
type InitFn = unsafe extern "C" fn() -> c_int;
type UnloadFn = unsafe extern "C" fn() -> c_int;
type GetErrMsgFn = unsafe extern "C" fn(c_int, *mut c_char) -> c_int;
type EnumGpusFn = unsafe extern "C" fn(*mut NvapiHandle, *mut u32) -> c_int;
type GetBusIdFn = unsafe extern "C" fn(NvapiHandle, *mut u32) -> c_int;
type GetVoltageFn = unsafe extern "C" fn(NvapiHandle, *mut NvApiVoltage) -> c_int;
type GetVfInfoFn = unsafe extern "C" fn(NvapiHandle, *mut VfPointsInfoV1) -> c_int;
type GetVfStatusFn = unsafe extern "C" fn(NvapiHandle, *mut VfPointsStatusV3) -> c_int;
type VfControlFn = unsafe extern "C" fn(NvapiHandle, *mut VfPointsControlV1) -> c_int;

// --- version-tagged structs (sizes asserted against ctypes `sizeof`) -------

/// `NvApiVoltage` (76 bytes; `value_uv` at offset 40).
#[repr(C)]
struct NvApiVoltage {
    version: u32,
    flags: u32,
    padding_1: [u32; 8],
    value_uv: u32,
    padding_2: [u32; 8],
}
const _: () = assert!(size_of::<NvApiVoltage>() == 76);

/// `ClockClientClkVfPointTupleV1` (40 bytes).
#[repr(C)]
#[derive(Clone, Copy)]
struct VfPointTupleV1 {
    freq_khz: u32,
    voltage_uv: u32,
    rsvd: [u8; 32],
}
const _: () = assert!(size_of::<VfPointTupleV1>() == 40);

/// `ClockClientClkVfPointStatusV3` (348 bytes).
#[repr(C)]
#[derive(Clone, Copy)]
struct VfPointStatusV3 {
    type_: u32,
    freq_khz: u32,
    voltage_uv: u32,
    vf_tuple_base: VfPointTupleV1,
    vf_tuple_offset: VfPointTupleV1,
    rsvd: [u8; 256],
}
const _: () = assert!(size_of::<VfPointStatusV3>() == 348);

/// `ClockClientClkVfPointsStatusV3` (88844 bytes; points at offset 104).
#[repr(C)]
struct VfPointsStatusV3 {
    version: u32,
    vf_points_mask: [u32; 8],
    b_vf_tuple_base_supported: u8,
    rsvd: [u8; 64],
    vf_points: [VfPointStatusV3; VF_POINTS_CAPACITY],
}
const _: () = assert!(size_of::<VfPointsStatusV3>() == 88844);

/// `ClockClientClkVfPointInfoV1` (24 bytes).
#[repr(C)]
#[derive(Clone, Copy)]
struct VfPointInfoV1 {
    type_: u32,
    b_voltage_based: u8,
    rsvd: [u8; 16],
}
const _: () = assert!(size_of::<VfPointInfoV1>() == 24);

/// `ClockClientClkVfPointsInfoV1` (6188 bytes; points at offset 68).
#[repr(C)]
struct VfPointsInfoV1 {
    version: u32,
    vf_points_mask: [u32; 8],
    rsvd: [u8; 32],
    vf_points: [VfPointInfoV1; VF_POINTS_CAPACITY],
}
const _: () = assert!(size_of::<VfPointsInfoV1>() == 6188);

/// The control union's `prog` member (`freq_offset_khz`) at offset 0, padded to
/// the union's 16-byte footprint. Modeled as a struct because only `.prog` is
/// ever touched; layout (16 bytes, i32 at offset 0) matches the C union.
#[repr(C)]
#[derive(Clone, Copy)]
struct VfPointControlDataV1 {
    prog_freq_offset_khz: i32,
    _rsvd: [u8; 12],
}
const _: () = assert!(size_of::<VfPointControlDataV1>() == 16);

/// `ClockClientClkVfPointControlV1` (36 bytes).
#[repr(C)]
#[derive(Clone, Copy)]
struct VfPointControlV1 {
    type_: u32,
    rsvd: [u8; 16],
    data: VfPointControlDataV1,
}
const _: () = assert!(size_of::<VfPointControlV1>() == 36);

/// `ClockClientClkVfPointsControlV1` (9248 bytes; points at offset 68).
#[repr(C)]
struct VfPointsControlV1 {
    version: u32,
    vf_points_mask: [u32; 8],
    rsvd: [u8; 32],
    vf_points: [VfPointControlV1; VF_POINTS_CAPACITY],
}
const _: () = assert!(size_of::<VfPointsControlV1>() == 9248);

/// `version = (sizeof & 0xFFFF) | (api_version << 16)`.
const fn make_version(size: usize, api_version: u32) -> u32 {
    ((size as u32) & 0xFFFF) | (api_version << 16)
}

/// Bus number from an NVML PCI bus id (`pci_bus_number_from_bus_id`, §1).
pub(crate) fn pci_bus_number_from_bus_id(pci_bus_id: &str) -> Option<u32> {
    let text = pci_bus_id.trim();
    if text.is_empty() {
        return None;
    }
    let parts: Vec<&str> = text.split(':').collect();
    let bus_text: &str = if parts.len() >= 2 {
        parts[parts.len() - 2]
    } else {
        // `(^|[^0-9a-fA-F])([0-9a-fA-F]{2})(?=[:._-])`: two hex digits preceded
        // by start/non-hex and followed by a separator.
        let bytes = text.as_bytes();
        let is_hex = |c: u8| c.is_ascii_hexdigit();
        let is_sep = |c: u8| matches!(c, b':' | b'.' | b'_' | b'-');
        let mut found = "";
        let mut i = 0;
        while i + 2 < bytes.len() {
            let prev_ok = i == 0 || !is_hex(bytes[i - 1]);
            if prev_ok && is_hex(bytes[i]) && is_hex(bytes[i + 1]) && is_sep(bytes[i + 2]) {
                found = &text[i..i + 2];
                break;
            }
            i += 1;
        }
        found
    };
    u32::from_str_radix(bus_text.trim(), 16).ok()
}

/// Fetch a query-interface pointer, erroring on NULL.
///
/// # Safety
/// `query` must be the real `nvapi_QueryInterface`, and `T` the correct
/// `extern "C"` fn type for `id`.
unsafe fn query_interface<T: Copy>(query: QueryFn, id: u32) -> Result<T, GpuError> {
    // SAFETY: `query` is `nvapi_QueryInterface`; a non-null return is a valid
    // function pointer for `id`, and `T` is pointer-sized like the returned
    // `*mut c_void`.
    let ptr = unsafe { query(id) };
    if ptr.is_null() {
        return Err(GpuError::other(
            format!("nvapi_QueryInterface({id:#x}) returned NULL"),
            0,
        ));
    }
    // SAFETY: fn pointers and data pointers share a representation on this ABI;
    // `T` is a bare `extern "C"` fn pointer of the same size.
    Ok(unsafe { std::mem::transmute_copy::<*mut c_void, T>(&ptr) })
}

/// As `query_interface` but NULL is tolerated (`get_bus_id` is optional).
///
/// # Safety
/// Same contract as [`query_interface`].
unsafe fn try_query_interface<T: Copy>(query: QueryFn, id: u32) -> Option<T> {
    // SAFETY: delegated to `query_interface`.
    unsafe { query_interface(query, id) }.ok()
}

/// Decode an `NvAPI_Status` into text (`status_text`, §2).
fn status_text(get_err: GetErrMsgFn, status: c_int) -> String {
    let mut buf = [0u8; NVAPI_SHORT_STRING_MAX];
    // SAFETY: `get_err` writes a NUL-terminated string into a 64-byte buffer.
    let rc = unsafe { get_err(status, buf.as_mut_ptr().cast::<c_char>()) };
    if rc == 0 {
        decode_cstr(&buf)
    } else {
        format!("status={status}")
    }
}

/// Set bits of a 256-bit mask → active point indices (`_mask_to_indices`).
fn mask_to_indices(mask: &[u32; 8]) -> Vec<u32> {
    let mut indices = Vec::new();
    for (word_index, &word) in mask.iter().enumerate() {
        for bit in 0..32 {
            if word & (1 << bit) != 0 {
                indices.push((word_index as u32) * 32 + bit);
            }
        }
    }
    indices
}

/// Enumerate physical GPUs and select the handle whose bus matches (else fall
/// back to `gpu_index`). Shared by both readers (`_select_gpu_handle`, §2).
fn select_gpu(
    enum_fn: EnumGpusFn,
    get_bus: Option<GetBusIdFn>,
    get_err: GetErrMsgFn,
    gpu_index: u32,
    requested_bus: Option<u32>,
) -> Result<NvapiHandle, GpuError> {
    let mut handles: [NvapiHandle; NVAPI_MAX_PHYSICAL_GPUS] =
        [std::ptr::null_mut(); NVAPI_MAX_PHYSICAL_GPUS];
    let mut count: u32 = 0;
    // SAFETY: `handles` holds 64 slots and `count` is a valid out-pointer.
    let rc = unsafe { enum_fn(handles.as_mut_ptr(), &mut count) };
    if rc != 0 || count == 0 {
        return Err(GpuError::nvapi(
            "NvAPI_EnumPhysicalGPUs",
            i64::from(rc),
            &status_text(get_err, rc),
        ));
    }
    if gpu_index >= count {
        return Err(GpuError::other(
            format!("GPU index {gpu_index} is out of range for {count} GPU(s)"),
            0,
        ));
    }
    if let (Some(bus), Some(get_bus)) = (requested_bus, get_bus) {
        for handle in handles.iter().take(count as usize) {
            let mut bus_id: u32 = 0;
            // SAFETY: `handle` came from the enumeration; `bus_id` is a valid
            // out-pointer.
            let rc = unsafe { get_bus(*handle, &mut bus_id) };
            if rc == 0 && bus_id == bus {
                return Ok(*handle);
            }
        }
    }
    Ok(handles[gpu_index as usize])
}

// ------------------------------------------------------------------------
// Telemetry reader (voltage and optional hotspot share one driver session).
// ------------------------------------------------------------------------
pub(crate) struct NvapiTelemetrySession {
    _lib: Library,
    gpu: NvapiHandle,
    unload: UnloadFn,
    get_err: GetErrMsgFn,
    get_voltage: Option<GetVoltageFn>,
    hotspot: Option<HotspotReader>,
    last_raw_uv: Cell<Option<i64>>,
    initialized: bool,
}

impl NvapiTelemetrySession {
    pub(crate) fn open(
        gpu_index: u32,
        pci_bus_id: &str,
        architecture: Option<u32>,
    ) -> Result<Self, GpuError> {
        let requested_bus = pci_bus_number_from_bus_id(pci_bus_id);
        // SAFETY: trusted system soname; the resolved query fn is used to bind
        // the entry points to their declared signatures below.
        let lib = unsafe { Library::new("libnvidia-api.so.1") }
            .map_err(|e| GpuError::other(format!("failed to load libnvidia-api.so.1: {e}"), 0))?;
        // SAFETY: `nvapi_QueryInterface` is the single exported symbol.
        let query: QueryFn = unsafe {
            let sym: Symbol<QueryFn> = lib.get(b"nvapi_QueryInterface\0").map_err(|e| {
                GpuError::other(format!("nvapi_QueryInterface unavailable: {e}"), 0)
            })?;
            *sym
        };

        // SAFETY: each id/type pair matches the NVAPI contract in spec 03.
        let (init, unload, get_err, enum_fn, get_bus, get_voltage) = unsafe {
            let init: InitFn = query_interface(query, Q_INITIALIZE)?;
            let unload: UnloadFn = query_interface(query, Q_UNLOAD)?;
            let get_err: GetErrMsgFn = query_interface(query, Q_GET_ERROR_MESSAGE)?;
            let enum_fn: EnumGpusFn = query_interface(query, Q_ENUM_PHYSICAL_GPUS)?;
            let get_bus: Option<GetBusIdFn> = try_query_interface(query, Q_GET_BUS_ID);
            let get_voltage: Option<GetVoltageFn> = try_query_interface(query, Q_VOLTAGE);
            (init, unload, get_err, enum_fn, get_bus, get_voltage)
        };

        // SAFETY: `init` is NvAPI_Initialize.
        let rc = unsafe { init() };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "NvAPI_Initialize",
                i64::from(rc),
                &status_text(get_err, rc),
            ));
        }
        // Initialized: on any later failure, unload before returning (mirrors
        // the voltage reader's `close()`-then-reraise).
        let gpu = match select_gpu(enum_fn, get_bus, get_err, gpu_index, requested_bus) {
            Ok(gpu) => gpu,
            Err(e) => {
                // SAFETY: `unload` pairs with the successful `init`.
                unsafe { unload() };
                return Err(e);
            }
        };

        // SAFETY: the library stays loaded and `gpu` belongs to this session.
        let hotspot = unsafe { HotspotReader::open(query, gpu, architecture) };
        Ok(Self {
            _lib: lib,
            gpu,
            unload,
            get_err,
            get_voltage,
            hotspot,
            last_raw_uv: Cell::new(None),
            initialized: true,
        })
    }

    /// Raw microvolts (propagates on failure, like `read_raw_microvolts`).
    fn read_raw_microvolts(&self) -> GpuResult<i64> {
        let get_voltage = self
            .get_voltage
            .ok_or_else(|| GpuError::other("NVAPI voltage query unavailable", 0))?;
        let mut data: NvApiVoltage =
            // SAFETY: every field is a POD integer; all-zero is a valid value.
            unsafe { std::mem::zeroed() };
        data.version = make_version(size_of::<NvApiVoltage>(), 1);
        // SAFETY: `gpu` is a valid handle; `data` is a correctly-sized,
        // version-tagged struct.
        let rc = unsafe { get_voltage(self.gpu, &mut data) };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "NvAPI voltage query",
                i64::from(rc),
                &status_text(self.get_err, rc),
            ));
        }
        let uv = i64::from(data.value_uv);
        self.last_raw_uv.set(Some(uv));
        Ok(uv)
    }

    /// Clamped microvolts; `None` if implausible or the query failed. The
    /// telemetry caller swallows read errors, so this method does too (parity).
    pub(crate) fn read_microvolts(&self) -> Option<i64> {
        match self.read_raw_microvolts() {
            Ok(uv) if voltage_uv_in_window(uv) => Some(uv),
            _ => None,
        }
    }

    pub(crate) fn read_hotspot_c(&self) -> Option<f64> {
        // SAFETY: the reader's functions and this handle share the live library.
        unsafe { self.hotspot.as_ref()?.read(self.gpu) }
    }
}

impl Drop for NvapiTelemetrySession {
    fn drop(&mut self) {
        if self.initialized {
            // SAFETY: `unload` pairs with the `init` performed in `open`.
            unsafe { (self.unload)() };
        }
    }
}

// ------------------------------------------------------------------------
// VF curve reader (`HiddenNvapiVfCurveReader`).
// ------------------------------------------------------------------------
pub(crate) struct NvapiVfSession {
    _lib: Library,
    gpu: NvapiHandle,
    unload: UnloadFn,
    get_err: GetErrMsgFn,
    get_vf_info: GetVfInfoFn,
    get_vf_status: GetVfStatusFn,
    get_vf_control: VfControlFn,
    set_vf_control: VfControlFn,
    mask: RefCell<[u32; 8]>,
    points: RefCell<Vec<VfPoint>>,
    initialized: bool,
}

impl NvapiVfSession {
    pub(crate) fn open(gpu_index: u32, pci_bus_id: &str) -> Result<Self, GpuError> {
        let requested_bus = pci_bus_number_from_bus_id(pci_bus_id);
        // SAFETY: trusted system soname.
        let lib = unsafe { Library::new("libnvidia-api.so.1") }
            .map_err(|e| GpuError::other(format!("failed to load libnvidia-api.so.1: {e}"), 0))?;
        // SAFETY: single exported symbol.
        let query: QueryFn = unsafe {
            let sym: Symbol<QueryFn> = lib.get(b"nvapi_QueryInterface\0").map_err(|e| {
                GpuError::other(format!("nvapi_QueryInterface unavailable: {e}"), 0)
            })?;
            *sym
        };

        // SAFETY: each id/type pair matches the NVAPI contract in spec 03.
        let (init, unload, get_err, enum_fn, get_bus, info, status, get_ctl, set_ctl) = unsafe {
            let init: InitFn = query_interface(query, Q_INITIALIZE)?;
            let unload: UnloadFn = query_interface(query, Q_UNLOAD)?;
            let get_err: GetErrMsgFn = query_interface(query, Q_GET_ERROR_MESSAGE)?;
            let enum_fn: EnumGpusFn = query_interface(query, Q_ENUM_PHYSICAL_GPUS)?;
            let get_bus: Option<GetBusIdFn> = try_query_interface(query, Q_GET_BUS_ID);
            let info: GetVfInfoFn = query_interface(query, Q_GET_VF_INFO)?;
            let status: GetVfStatusFn = query_interface(query, Q_GET_VF_STATUS)?;
            let get_ctl: VfControlFn = query_interface(query, Q_GET_VF_CONTROL)?;
            let set_ctl: VfControlFn = query_interface(query, Q_SET_VF_CONTROL)?;
            (
                init, unload, get_err, enum_fn, get_bus, info, status, get_ctl, set_ctl,
            )
        };

        // SAFETY: `init` is NvAPI_Initialize.
        let rc = unsafe { init() };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "NvAPI_Initialize",
                i64::from(rc),
                &status_text(get_err, rc),
            ));
        }
        let gpu = match select_gpu(enum_fn, get_bus, get_err, gpu_index, requested_bus) {
            Ok(gpu) => gpu,
            Err(e) => {
                // SAFETY: `unload` pairs with the successful `init`.
                unsafe { unload() };
                return Err(e);
            }
        };

        let session = Self {
            _lib: lib,
            gpu,
            unload,
            get_err,
            get_vf_info: info,
            get_vf_status: status,
            get_vf_control: get_ctl,
            set_vf_control: set_ctl,
            mask: RefCell::new([0; 8]),
            points: RefCell::new(Vec::new()),
            initialized: true,
        };
        // On failure `session` is dropped here, running `Drop` (unload).
        session.refresh_points()?;
        Ok(session)
    }

    fn get_info(&self) -> GpuResult<Box<VfPointsInfoV1>> {
        // SAFETY: POD struct; all-zero is valid.
        let mut info: Box<VfPointsInfoV1> = Box::new(unsafe { std::mem::zeroed() });
        info.version = make_version(size_of::<VfPointsInfoV1>(), 1);
        // SAFETY: `gpu` valid; `info` correctly sized and version-tagged.
        let rc = unsafe { (self.get_vf_info)(self.gpu, info.as_mut()) };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "ClockClientClkVfPointsGetInfo",
                i64::from(rc),
                &status_text(self.get_err, rc),
            ));
        }
        Ok(info)
    }

    fn get_status(&self, mask: &[u32; 8]) -> GpuResult<Box<VfPointsStatusV3>> {
        // SAFETY: POD struct; all-zero is valid.
        let mut status: Box<VfPointsStatusV3> = Box::new(unsafe { std::mem::zeroed() });
        status.version = make_version(size_of::<VfPointsStatusV3>(), 3);
        status.vf_points_mask = *mask;
        // SAFETY: `gpu` valid; `status` correctly sized and version-tagged.
        let rc = unsafe { (self.get_vf_status)(self.gpu, status.as_mut()) };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "ClockClientClkVfPointsGetStatus",
                i64::from(rc),
                &status_text(self.get_err, rc),
            ));
        }
        Ok(status)
    }

    fn get_control(&self, mask: &[u32; 8]) -> GpuResult<Box<VfPointsControlV1>> {
        // SAFETY: POD struct; all-zero is valid.
        let mut control: Box<VfPointsControlV1> = Box::new(unsafe { std::mem::zeroed() });
        control.version = make_version(size_of::<VfPointsControlV1>(), 1);
        control.vf_points_mask = *mask;
        // SAFETY: `gpu` valid; `control` correctly sized and version-tagged.
        let rc = unsafe { (self.get_vf_control)(self.gpu, control.as_mut()) };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "ClockClientClkVfPointsGetControl",
                i64::from(rc),
                &status_text(self.get_err, rc),
            ));
        }
        Ok(control)
    }

    fn set_control(&self, control: &mut VfPointsControlV1) -> GpuResult<()> {
        // SAFETY: `gpu` valid; `control` is a live, version-tagged struct.
        let rc = unsafe { (self.set_vf_control)(self.gpu, control) };
        if rc != 0 {
            return Err(GpuError::nvapi(
                "ClockClientClkVfPointsSetControl",
                i64::from(rc),
                &status_text(self.get_err, rc),
            ));
        }
        Ok(())
    }

    /// Re-read the whole curve into the cache (`_read_points`/`refresh_points`).
    /// Consumers read the fresh points back with [`Self::points`].
    pub(crate) fn refresh_points(&self) -> GpuResult<()> {
        let info = self.get_info()?;
        let mask = info.vf_points_mask;
        let status = self.get_status(&mask)?;
        let control = self.get_control(&mask)?;
        *self.mask.borrow_mut() = mask;

        let mut points = Vec::new();
        for idx in mask_to_indices(&mask) {
            let i = idx as usize;
            // The mask spans 256 bits but the arrays hold 255 points; a bit at
            // index 255 (never seen in practice) is skipped rather than read OOB.
            if i >= VF_POINTS_CAPACITY {
                continue;
            }
            let info_p = &info.vf_points[i];
            let st = &status.vf_points[i];
            let ct = &control.vf_points[i];
            points.push(VfPoint {
                index: idx,
                type_: info_p.type_,
                voltage_based: u32::from(info_p.b_voltage_based),
                freq_khz: i64::from(st.freq_khz),
                voltage_uv: i64::from(st.voltage_uv),
                base_freq_khz: i64::from(st.vf_tuple_base.freq_khz),
                base_voltage_uv: i64::from(st.vf_tuple_base.voltage_uv),
                current_offset_khz: i64::from(ct.data.prog_freq_offset_khz),
            });
        }
        *self.points.borrow_mut() = points;
        Ok(())
    }

    pub(crate) fn points(&self) -> Vec<VfPoint> {
        self.points.borrow().clone()
    }

    pub(crate) fn editable_core_points(&self) -> Vec<VfPoint> {
        vf_editable_core_points(&self.points.borrow())
    }

    pub(crate) fn find_nearest_point(
        &self,
        core_clock_mhz: i64,
        voltage_uv: i64,
    ) -> Option<VfPoint> {
        vf_find_nearest(&self.points.borrow(), core_clock_mhz, voltage_uv)
    }

    pub(crate) fn summary(&self) -> VfSummary {
        vf_summary_of(&self.points.borrow())
    }

    /// Read the live control struct, set `freq_offset_khz` for each requested
    /// point, and write it back with a single `SetControl` (the `apply_plan`
    /// path). Caller verifies persistence via `refresh_points`.
    pub(crate) fn apply_offsets_khz(&self, offsets: &[(u32, i32)]) -> GpuResult<()> {
        let mask = *self.mask.borrow();
        let mut control = self.get_control(&mask)?;
        for &(index, offset_khz) in offsets {
            let i = index as usize;
            if i < VF_POINTS_CAPACITY {
                control.vf_points[i].data.prog_freq_offset_khz = offset_khz;
            }
        }
        self.set_control(&mut control)
    }
}

impl Drop for NvapiVfSession {
    fn drop(&mut self) {
        if self.initialized {
            // SAFETY: `unload` pairs with the `init` performed in `open`.
            unsafe { (self.unload)() };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bus_number_from_standard_bus_id() {
        // domain:bus:device.func -> parts[-2] is the bus hex.
        assert_eq!(pci_bus_number_from_bus_id("00000000:2B:00.0"), Some(0x2B));
        assert_eq!(pci_bus_number_from_bus_id("0000:01:00.0"), Some(1));
        assert_eq!(pci_bus_number_from_bus_id("  0000:2b:00.0  "), Some(0x2B));
    }

    #[test]
    fn bus_number_edge_cases() {
        assert_eq!(pci_bus_number_from_bus_id(""), None);
        assert_eq!(pci_bus_number_from_bus_id("   "), None);
        // No colon -> regex-style scan for two hex digits before a separator.
        assert_eq!(pci_bus_number_from_bus_id("2b.00"), Some(0x2B));
        // Not parseable as hex -> None.
        assert_eq!(pci_bus_number_from_bus_id("zz:zz:zz"), None);
    }

    #[test]
    fn make_version_matches_ctypes() {
        // Values computed from ctypes.sizeof on this box.
        assert_eq!(make_version(76, 1), 0x0001_004C);
        assert_eq!(make_version(size_of::<VfPointsInfoV1>(), 1), 0x0001_182C);
        assert_eq!(make_version(size_of::<VfPointsStatusV3>(), 3), 0x0003_5B0C);
        assert_eq!(make_version(size_of::<VfPointsControlV1>(), 1), 0x0001_2420);
    }

    #[test]
    fn mask_decode() {
        // bit 0 of word 0, bit 1 of word 0, bit 0 of word 1 (index 32).
        let mask = [0b11u32, 0b1, 0, 0, 0, 0, 0, 0];
        assert_eq!(mask_to_indices(&mask), vec![0, 1, 32]);
    }
}
