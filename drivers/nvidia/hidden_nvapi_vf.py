from __future__ import annotations

import ctypes

from runtime.daemon_client import gpu_apply_vf_offsets

from .daemon_gpu import DaemonGpuClient
from .hidden_nvapi_gpu_selection import (
    pci_bus_number_from_bus_id,
)

NvAPI_Status = ctypes.c_int32
NvU32 = ctypes.c_uint32
NvS32 = ctypes.c_int32
NvU8 = ctypes.c_uint8
NvPhysicalGpuHandle = ctypes.c_void_p

NVAPI_MAX_PHYSICAL_GPUS = 64
NVAPI_SHORT_STRING_MAX = 64
VF_POINTS_CAPACITY = 255


class ClockClientClkVfPointTupleV1(ctypes.Structure):
    _fields_ = [
        ("freq_khz", NvU32),
        ("voltage_uv", NvU32),
        ("rsvd", NvU8 * 32),
    ]


class ClockClientClkVfPointStatusV3(ctypes.Structure):
    _fields_ = [
        ("type_", NvU32),
        ("freq_khz", NvU32),
        ("voltage_uv", NvU32),
        ("vf_tuple_base", ClockClientClkVfPointTupleV1),
        ("vf_tuple_offset", ClockClientClkVfPointTupleV1),
        ("rsvd", NvU8 * 256),
    ]


class ClockClientClkVfPointsStatusV3(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("b_vf_tuple_base_supported", NvU8),
        ("rsvd", NvU8 * 64),
        ("vf_points", ClockClientClkVfPointStatusV3 * VF_POINTS_CAPACITY),
    ]


class ClockClientClkVfPointInfoV1(ctypes.Structure):
    _fields_ = [
        ("type_", NvU32),
        ("b_voltage_based", NvU8),
        ("rsvd", NvU8 * 16),
    ]


class ClockClientClkVfPointsInfoV1(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("rsvd", NvU8 * 32),
        ("vf_points", ClockClientClkVfPointInfoV1 * VF_POINTS_CAPACITY),
    ]


class ClockClientClkVfPointControlProgV1(ctypes.Structure):
    _fields_ = [("freq_offset_khz", NvS32)]


class ClockClientClkVfPointControlDataV1(ctypes.Union):
    _fields_ = [
        ("prog", ClockClientClkVfPointControlProgV1),
        ("rsvd", NvU8 * 16),
    ]


class ClockClientClkVfPointControlV1(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [
        ("type_", NvU32),
        ("rsvd", NvU8 * 16),
        ("data", ClockClientClkVfPointControlDataV1),
    ]


class ClockClientClkVfPointsControlV1(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("rsvd", NvU8 * 32),
        ("vf_points", ClockClientClkVfPointControlV1 * VF_POINTS_CAPACITY),
    ]


def _daemon_pci_bus_id(gpu_index: int) -> str:
    try:
        return DaemonGpuClient(gpu_index).capabilities().identity.pci_bus_id
    except Exception:  # noqa: BLE001
        return ""


class HiddenNvapiVfCurveReader:
    _QUERY_INITIALIZE = 0x0150E828
    _QUERY_UNLOAD = 0xD22BDD7E
    _QUERY_GET_ERROR_MESSAGE = 0x6C2D048C
    _QUERY_ENUM_PHYSICAL_GPUS = 0xE5AC921F
    _QUERY_GET_BUS_ID = 0x1BE0B8E5
    _QUERY_GET_VF_INFO = 0x507B4B59
    _QUERY_GET_VF_STATUS = 0x21537AD4
    _QUERY_GET_VF_CONTROL = 0x23F1B133

    def __init__(self, gpu_index=0, *, pci_bus_id: str = ""):
        self._gpu_index = int(gpu_index)
        self._requested_pci_bus_id = str(
            pci_bus_id or ""
        ).strip() or _daemon_pci_bus_id(self._gpu_index)
        self._requested_bus_number = pci_bus_number_from_bus_id(
            self._requested_pci_bus_id
        )
        self._lib = ctypes.CDLL("libnvidia-api.so.1")
        self._initialize = self._query_interface(self._QUERY_INITIALIZE, NvAPI_Status)
        self._unload = self._query_interface(self._QUERY_UNLOAD, NvAPI_Status)
        self._get_error_message = self._query_interface(
            self._QUERY_GET_ERROR_MESSAGE,
            NvAPI_Status,
            NvAPI_Status,
            ctypes.c_char_p,
        )
        self._enum_physical_gpus = self._query_interface(
            self._QUERY_ENUM_PHYSICAL_GPUS,
            NvAPI_Status,
            ctypes.POINTER(NvPhysicalGpuHandle),
            ctypes.POINTER(NvU32),
        )
        self._get_bus_id = self._try_query_interface(
            self._QUERY_GET_BUS_ID,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(NvU32),
        )
        self._get_vf_info = self._query_interface(
            self._QUERY_GET_VF_INFO,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsInfoV1),
        )
        self._get_vf_status = self._query_interface(
            self._QUERY_GET_VF_STATUS,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsStatusV3),
        )
        self._get_vf_control = self._query_interface(
            self._QUERY_GET_VF_CONTROL,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsControlV1),
        )
        self._initialized = False
        self._gpu = None
        self._mask: list[int] = []
        self._initialize_session()
        self._points = self._read_points()

    def _query_interface(self, interface_id, restype, *argtypes):
        query = self._lib.nvapi_QueryInterface
        query.argtypes = [NvU32]
        query.restype = ctypes.c_void_p
        ptr = query(interface_id)
        if not ptr:
            raise RuntimeError(f"nvapi_QueryInterface({interface_id:#x}) returned NULL")
        return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)

    def _try_query_interface(self, interface_id, restype, *argtypes):
        try:
            return self._query_interface(interface_id, restype, *argtypes)
        except RuntimeError:
            return None

    @staticmethod
    def _make_version(struct_type, version):
        return (ctypes.sizeof(struct_type) & 0xFFFF) | (version << 16)

    @staticmethod
    def _mask_to_indices(mask_words):
        indices = []
        for word_index, word in enumerate(mask_words):
            value = int(word)
            for bit in range(32):
                if value & (1 << bit):
                    indices.append(word_index * 32 + bit)
        return indices

    def _initialize_session(self):
        if self._initialized:
            return
        rc = int(self._initialize())
        if rc != 0:
            raise RuntimeError(
                f"NvAPI_Initialize failed with status {rc}: {self.status_text(rc)}"
            )
        self._initialized = True

        handles = (NvPhysicalGpuHandle * NVAPI_MAX_PHYSICAL_GPUS)()
        count = NvU32(0)
        rc = int(self._enum_physical_gpus(handles, ctypes.byref(count)))
        if rc != 0 or count.value == 0:
            raise RuntimeError(
                f"NvAPI_EnumPhysicalGPUs failed with status {rc}: {self.status_text(rc)}"
            )

        if self._gpu_index >= count.value:
            raise RuntimeError(
                f"GPU index {self._gpu_index} is out of range for {count.value} GPU(s)"
            )

        self._gpu = self._select_gpu_handle(handles, count.value)

    def _select_gpu_handle(self, handles, count: int):
        requested_bus = self._requested_bus_number
        if requested_bus is not None and self._get_bus_id is not None:
            for index in range(int(count)):
                bus_id = NvU32(0)
                rc = int(self._get_bus_id(handles[index], ctypes.byref(bus_id)))
                if rc == 0 and int(bus_id.value) == int(requested_bus):
                    return handles[index]
        return handles[self._gpu_index]

    def _read_points(self):
        if not self._initialized or self._gpu is None:
            raise RuntimeError("NVAPI VF session is not initialized")

        info = self.get_info_struct()
        status = self.get_status_struct(info.vf_points_mask)
        control = self.get_control_struct(info.vf_points_mask)

        self._mask = [int(x) for x in info.vf_points_mask]

        points = []
        for idx in self._mask_to_indices(self._mask):
            point_info = info.vf_points[idx]
            point_status = status.vf_points[idx]
            point_control = control.vf_points[idx]
            points.append(
                {
                    "index": idx,
                    "type": int(point_info.type_),
                    "voltage_based": int(point_info.b_voltage_based),
                    "freq_khz": int(point_status.freq_khz),
                    "voltage_uv": int(point_status.voltage_uv),
                    # Turing may leave the optional base tuple empty.
                    "base_freq_khz": int(point_status.vf_tuple_base.freq_khz)
                    or int(point_status.freq_khz),
                    "base_voltage_uv": int(point_status.vf_tuple_base.voltage_uv)
                    or int(point_status.voltage_uv),
                    "current_offset_khz": int(point_control.prog.freq_offset_khz),
                }
            )

        return points

    def status_text(self, status):
        buf = ctypes.create_string_buffer(NVAPI_SHORT_STRING_MAX)
        rc = int(self._get_error_message(NvAPI_Status(status), buf))
        if rc == 0:
            return buf.value.decode(errors="replace")
        return f"status={status}"

    def get_info_struct(self):
        info = ClockClientClkVfPointsInfoV1()
        info.version = self._make_version(ClockClientClkVfPointsInfoV1, 1)
        rc = int(self._get_vf_info(self._gpu, ctypes.byref(info)))
        if rc != 0:
            raise RuntimeError(
                f"ClockClientClkVfPointsGetInfo failed with status {rc}: {self.status_text(rc)}"
            )
        return info

    def get_status_struct(self, mask=None):
        if mask is None:
            mask = self._mask
        status = ClockClientClkVfPointsStatusV3()
        status.version = self._make_version(ClockClientClkVfPointsStatusV3, 3)
        for i, value in enumerate(mask):
            status.vf_points_mask[i] = value
        rc = int(self._get_vf_status(self._gpu, ctypes.byref(status)))
        if rc != 0:
            raise RuntimeError(
                f"ClockClientClkVfPointsGetStatus failed with status {rc}: {self.status_text(rc)}"
            )
        return status

    def get_control_struct(self, mask=None):
        if mask is None:
            mask = self._mask
        control = ClockClientClkVfPointsControlV1()
        control.version = self._make_version(ClockClientClkVfPointsControlV1, 1)
        for i, value in enumerate(mask):
            control.vf_points_mask[i] = value
        rc = int(self._get_vf_control(self._gpu, ctypes.byref(control)))
        if rc != 0:
            raise RuntimeError(
                f"ClockClientClkVfPointsGetControl failed with status {rc}: {self.status_text(rc)}"
            )
        return control

    def set_control_struct(self, control):
        # Milestone B: the privileged NVAPI SET routes through the root daemon.
        # Ship the full plan the local SET would have programmed -- every masked
        # point with its freq_offset_khz from `control` (which callers filled
        # from a fresh GET before mutating) -- so the daemon's get-mutate-set
        # produces byte-identical driver state. Error text is the backend's
        # exact "ClockClientClkVfPointsSetControl failed with status ..."
        # message, relayed verbatim as the RuntimeError message.
        offsets = [
            (index, int(control.vf_points[index].prog.freq_offset_khz))
            for index in self._mask_to_indices(
                [int(word) for word in control.vf_points_mask]
            )
        ]
        gpu_apply_vf_offsets(self._gpu_index, offsets)
        return 0

    def editable_core_points(self):
        return [
            point
            for point in self._points
            if point["type"] == 0 and point["voltage_based"] == 1
        ]

    def summary(self):
        editable = self.editable_core_points()
        return {
            "active_points": len(self._points),
            "editable_core_points": len(editable),
        }

    def refresh_points(self):
        self._points = self._read_points()
        return self._points

    def close(self):
        if self._initialized:
            self._unload()
            self._initialized = False


def create_hidden_vf_curve_reader(gpu_index=0):
    try:
        return HiddenNvapiVfCurveReader(gpu_index=gpu_index)
    except Exception:  # noqa: BLE001
        return None
