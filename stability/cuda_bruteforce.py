"""Deterministic CUDA stability workload tuned to fail softly.

A marginal undervolt should surface as a wrong result or a GPU fault that the
scan can blacklist, not as a bus drop that takes the host down. Three things
push the odds that way:

* every launch runs twice and the two copies are compared element for element
  on the device, so a single flaky SM or lane is caught at once instead of
  being missed by a handful of spot checks (the integer chain stays the only
  stress kernel: FP32 FMA chains draw a quarter less power and would weaken
  the test);
* the load ramps through four occupancy stages, so a marginal point tends to
  produce errors at partial current, while the chip is still answering,
  before the full-current stage can hang it;
* a host watchdog turns a stalled GPU into an exit code while the device is
  still on the bus.

Exit codes: 0 stable, 1 setup or driver error, 3 instability (mismatch or GPU
fault during the stress), 4 the GPU stopped answering.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass

PTX_SOURCE = rb"""
.version 6.0
.target sm_52
.address_size 64

.visible .entry brute_force_kernel(
    .param .u64 out_x,
    .param .u64 out_y,
    .param .u32 n,
    .param .u32 rounds,
    .param .u32 seed0,
    .param .u32 seed1
)
{
    .reg .pred %p<3>;
    .reg .b32 %r<20>;
    .reg .b64 %rd<8>;

    ld.param.u64 %rd1, [out_x];
    ld.param.u64 %rd2, [out_y];
    ld.param.u32 %r1, [n];
    ld.param.u32 %r2, [rounds];
    ld.param.u32 %r3, [seed0];
    ld.param.u32 %r4, [seed1];

    mov.u32 %r5, %tid.x;
    mov.u32 %r6, %ctaid.x;
    mov.u32 %r7, %ntid.x;
    mov.u32 %r8, %nctaid.x;
    mad.lo.u32 %r9, %r6, %r7, %r5;
    mul.lo.u32 %r10, %r7, %r8;

LOOP_I:
    setp.ge.u32 %p1, %r9, %r1;
    @%p1 bra DONE;

    xor.b32 %r11, %r3, %r9;
    mad.lo.u32 %r12, %r9, 17, %r4;
    mov.u32 %r13, 0;

LOOP_R:
    setp.ge.u32 %p2, %r13, %r2;
    @%p2 bra STORE;

    mad.lo.u32 %r11, %r11, 1664525, 1013904223;
    add.u32 %r11, %r11, %r12;
    shr.u32 %r14, %r11, 13;
    xor.b32 %r12, %r12, %r14;
    mad.lo.u32 %r12, %r12, 22695477, 1;
    shl.b32 %r15, %r12, 7;
    xor.b32 %r11, %r11, %r15;
    shr.u32 %r16, %r11, 17;
    xor.b32 %r12, %r12, %r16;

    add.u32 %r13, %r13, 1;
    bra LOOP_R;

STORE:
    mul.wide.u32 %rd3, %r9, 4;
    add.s64 %rd4, %rd1, %rd3;
    st.global.u32 [%rd4], %r11;
    add.s64 %rd5, %rd2, %rd3;
    st.global.u32 [%rd5], %r12;
    add.u32 %r9, %r9, %r10;
    bra LOOP_I;

DONE:
    ret;
}

.visible .entry compare_u32_kernel(
    .param .u64 lhs,
    .param .u64 rhs,
    .param .u32 n,
    .param .u64 counter
)
{
    .reg .pred %p<3>;
    .reg .b32 %r<16>;
    .reg .b64 %rd<8>;

    ld.param.u64 %rd1, [lhs];
    ld.param.u64 %rd2, [rhs];
    ld.param.u32 %r1, [n];
    ld.param.u64 %rd3, [counter];

    mov.u32 %r5, %tid.x;
    mov.u32 %r6, %ctaid.x;
    mov.u32 %r7, %ntid.x;
    mov.u32 %r8, %nctaid.x;
    mad.lo.u32 %r9, %r6, %r7, %r5;
    mul.lo.u32 %r10, %r7, %r8;

CMP_LOOP:
    setp.ge.u32 %p1, %r9, %r1;
    @%p1 bra CMP_DONE;

    mul.wide.u32 %rd4, %r9, 4;
    add.s64 %rd5, %rd1, %rd4;
    add.s64 %rd6, %rd2, %rd4;
    ld.global.u32 %r11, [%rd5];
    ld.global.u32 %r12, [%rd6];
    setp.ne.u32 %p2, %r11, %r12;
    @%p2 atom.global.add.u32 %r13, [%rd3], 1;

    add.u32 %r9, %r9, %r10;
    bra CMP_LOOP;

CMP_DONE:
    ret;
}
"""

DEFAULT_STRESS_ELEMENTS = 4 * 1024 * 1024
DEFAULT_VERIFY_ELEMENTS = 4096
DEFAULT_STRESS_ROUNDS = 8192
DEFAULT_VERIFY_ROUNDS = 256
# Each batch launches this many seeds; every seed runs twice (two copies).
DEFAULT_STRESS_BATCH_LAUNCHES = 8
MAX_VERIFY_INTERVAL_S = 5.0
FULL_GRID_BLOCKS = 256
BLOCK_THREADS = 256
# Occupancy ramp: a marginal voltage tends to produce wrong results at partial
# current, while the chip still answers, before full current can hang it.
RAMP_FRACTIONS = (0.125, 0.25, 0.5, 1.0)
RAMP_STAGE_MIN_S = 0.3
RAMP_STAGE_MAX_S = 1.0
HANG_TIMEOUT_S = 8.0

EXIT_OK = 0
EXIT_SETUP_ERROR = 1
EXIT_INSTABILITY = 3
EXIT_HANG = 4
# Exit codes the scan must read as GPU instability, never as a setup problem.
CUDA_INSTABILITY_EXIT_CODES = frozenset({EXIT_INSTABILITY, EXIT_HANG})


@dataclass(frozen=True, slots=True)
class RampStage:
    fraction: float
    seconds: float

    @property
    def grid_blocks(self) -> int:
        return max(1, round(FULL_GRID_BLOCKS * float(self.fraction)))

    @property
    def elements(self) -> int:
        # Scale the work with the grid so a launch takes about the same time
        # at every stage and the compare cadence stays tight.
        return max(
            DEFAULT_VERIFY_ELEMENTS,
            int(DEFAULT_STRESS_ELEMENTS * float(self.fraction)),
        )


def ramp_schedule(duration_seconds: float) -> list[RampStage]:
    """Partial-occupancy stages first, then the full load for the remainder."""
    total = max(1.0, float(duration_seconds))
    partial_s = max(RAMP_STAGE_MIN_S, min(RAMP_STAGE_MAX_S, total / 10.0))
    stages: list[RampStage] = []
    remaining = total
    for fraction in RAMP_FRACTIONS[:-1]:
        seconds = min(partial_s, max(0.0, remaining - RAMP_STAGE_MIN_S))
        if seconds < RAMP_STAGE_MIN_S:
            break
        stages.append(RampStage(fraction, seconds))
        remaining -= seconds
    stages.append(RampStage(RAMP_FRACTIONS[-1], max(RAMP_STAGE_MIN_S, remaining)))
    return stages


class _Watchdog:
    """Turn a GPU that stopped answering into an exit while the host still can."""

    def __init__(
        self,
        timeout_s: float,
        on_hang,
        clock=time.monotonic,
        poll_s: float = 0.25,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.on_hang = on_hang
        self.clock = clock
        self.poll_s = float(poll_s)
        self._last_beat = clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cuda-watchdog", daemon=True)

    def beat(self) -> None:
        self._last_beat = self.clock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            stalled_s = self.clock() - self._last_beat
            if stalled_s > self.timeout_s:
                self.on_hang(stalled_s)
                return


def _exit_on_hang(stalled_s: float) -> None:
    _log(
        f"HANG launch timeout: no progress for {stalled_s:.1f}s; "
        "the GPU stopped answering (treated as instability)"
    )
    os._exit(EXIT_HANG)


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _cpu_reference(index: int, rounds: int, seed0: int, seed1: int) -> tuple[int, int]:
    x = _u32(seed0 ^ index)
    y = _u32(index * 17 + seed1)
    for _ in range(int(rounds)):
        x = _u32(x * 1664525 + 1013904223)
        x = _u32(x + y)
        y = _u32(y ^ (x >> 13))
        y = _u32(y * 22695477 + 1)
        x = _u32(x ^ ((y << 7) & 0xFFFFFFFF))
        y = _u32(y ^ (x >> 17))
    return x, y


def _stress_seed(base_seed: int, launch_index: int, stream_seed: int) -> int:
    return _u32(int(base_seed) + int(launch_index) * 0x9E3779B9 + int(stream_seed))


def _verification_sample_indices(element_count: int) -> list[int]:
    count = int(element_count)
    candidates = [
        0,
        1,
        2,
        7,
        31,
        255,
        1023,
        4095,
        count // 4,
        count // 2,
        (count * 3) // 4,
        count - 1,
    ]
    seen = set()
    indices = []
    for index in candidates:
        if index < 0 or index >= count or index in seen:
            continue
        seen.add(index)
        indices.append(index)
    return indices


def _verify_interval_s(duration_seconds: float) -> float:
    duration_s = max(1.0, float(duration_seconds))
    return max(1.0, min(MAX_VERIFY_INTERVAL_S, duration_s / 3.0))


class CudaDriverError(RuntimeError):
    pass


class CudaInstabilityError(CudaDriverError):
    """A wrong result or GPU fault while under load: the candidate is unsafe."""


class _CudaDriver:
    def __init__(self, lib=None) -> None:
        if lib is None:
            library_name = ctypes.util.find_library("cuda") or "libcuda.so.1"
            lib = ctypes.CDLL(library_name)
        self.lib = lib
        self._bind()

    def _bind(self) -> None:
        self.lib.cuInit.argtypes = [ctypes.c_uint]
        self.lib.cuInit.restype = ctypes.c_int
        self.lib.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        self.lib.cuDeviceGet.restype = ctypes.c_int
        self.lib.cuCtxCreate_v2.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
            ctypes.c_int,
        ]
        self.lib.cuCtxCreate_v2.restype = ctypes.c_int
        self.lib.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
        self.lib.cuCtxDestroy_v2.restype = ctypes.c_int
        self.lib.cuModuleLoadData.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        ]
        self.lib.cuModuleLoadData.restype = ctypes.c_int
        self.lib.cuModuleUnload.argtypes = [ctypes.c_void_p]
        self.lib.cuModuleUnload.restype = ctypes.c_int
        self.lib.cuModuleGetFunction.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        self.lib.cuModuleGetFunction.restype = ctypes.c_int
        self.lib.cuMemAlloc_v2.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_size_t,
        ]
        self.lib.cuMemAlloc_v2.restype = ctypes.c_int
        self.lib.cuMemFree_v2.argtypes = [ctypes.c_uint64]
        self.lib.cuMemFree_v2.restype = ctypes.c_int
        self.lib.cuMemcpyDtoH_v2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_size_t,
        ]
        self.lib.cuMemcpyDtoH_v2.restype = ctypes.c_int
        self.lib.cuMemsetD32_v2.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint,
            ctypes.c_size_t,
        ]
        self.lib.cuMemsetD32_v2.restype = ctypes.c_int
        self.lib.cuLaunchKernel.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        ]
        self.lib.cuLaunchKernel.restype = ctypes.c_int
        self.lib.cuCtxSynchronize.argtypes = []
        self.lib.cuCtxSynchronize.restype = ctypes.c_int
        cu_get_error_string = getattr(self.lib, "cuGetErrorString", None)
        if cu_get_error_string is not None:
            cu_get_error_string.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            cu_get_error_string.restype = ctypes.c_int

    def check(self, rc: int, context: str) -> None:
        if int(rc) == 0:
            return
        detail = ""
        cu_get_error_string = getattr(self.lib, "cuGetErrorString", None)
        if cu_get_error_string is not None:
            message = ctypes.c_char_p()
            if (
                int(cu_get_error_string(int(rc), ctypes.byref(message))) == 0
                and message.value
            ):
                detail = f": {message.value.decode(errors='replace')}"
        raise CudaDriverError(f"{context} failed rc={int(rc)}{detail}")


def _log(message: str) -> None:
    print(f"cuda-bruteforce: {message}", flush=True)


def _verify_outputs(
    driver: _CudaDriver,
    dev_x: ctypes.c_uint64,
    dev_y: ctypes.c_uint64,
    *,
    element_count: int,
    rounds: int,
    seed0: int,
    seed1: int,
    label: str,
) -> None:
    if int(element_count) <= 0:
        raise CudaDriverError(f"{label} verification needs positive element count")
    if dev_x.value is None or dev_y.value is None:
        raise CudaDriverError(f"{label} verification received null device pointer")
    host_x = ctypes.c_uint32()
    host_y = ctypes.c_uint32()
    sample_indices = _verification_sample_indices(int(element_count))
    checked = []
    for index in sample_indices:
        offset = int(index) * ctypes.sizeof(ctypes.c_uint32)
        driver.check(
            driver.lib.cuMemcpyDtoH_v2(
                ctypes.byref(host_x),
                ctypes.c_uint64(int(dev_x.value) + offset),
                ctypes.sizeof(host_x),
            ),
            f"cuMemcpyDtoH_v2({label}.x[{index}])",
        )
        driver.check(
            driver.lib.cuMemcpyDtoH_v2(
                ctypes.byref(host_y),
                ctypes.c_uint64(int(dev_y.value) + offset),
                ctypes.sizeof(host_y),
            ),
            f"cuMemcpyDtoH_v2({label}.y[{index}])",
        )
        expected_x, expected_y = _cpu_reference(
            index, int(rounds), int(seed0), int(seed1)
        )
        actual_x = int(host_x.value)
        actual_y = int(host_y.value)
        if actual_x != expected_x or actual_y != expected_y:
            raise CudaDriverError(
                f"{label} verification mismatch idx={index} "
                f"x={actual_x} expected_x={expected_x} "
                f"y={actual_y} expected_y={expected_y}"
            )
        checked.append(index)
    _log(f"verified {label} indices={checked}")


def _launch_params(*values: ctypes._SimpleCData) -> ctypes.Array:
    params = (ctypes.c_void_p * len(values))()
    for index, value in enumerate(values):
        params[index] = ctypes.cast(ctypes.byref(value), ctypes.c_void_p)
    return params


def _launch(
    driver: _CudaDriver,
    function: ctypes.c_void_p,
    params: ctypes.Array,
    *,
    grid_dim: int,
) -> None:
    driver.check(
        driver.lib.cuLaunchKernel(
            function,
            int(grid_dim), 1, 1,
            BLOCK_THREADS, 1, 1,
            0, None, params, None,
        ),
        "cuLaunchKernel",
    )


def _launch_kernel(
    driver: _CudaDriver,
    function: ctypes.c_void_p,
    *,
    out_x: ctypes.c_uint64,
    out_y: ctypes.c_uint64,
    element_count: int,
    rounds: int,
    seed0: int,
    seed1: int,
    grid_dim: int,
    block_dim: int = BLOCK_THREADS,
) -> None:
    del block_dim  # Every kernel uses BLOCK_THREADS.
    # The parameter objects must outlive the launch call.
    values = (
        ctypes.c_uint64(int(out_x.value)),
        ctypes.c_uint64(int(out_y.value)),
        ctypes.c_uint32(int(element_count)),
        ctypes.c_uint32(int(rounds)),
        ctypes.c_uint32(int(seed0)),
        ctypes.c_uint32(int(seed1)),
    )
    _launch(driver, function, _launch_params(*values), grid_dim=grid_dim)


def _count_mismatches(
    driver: _CudaDriver,
    compare: ctypes.c_void_p,
    *,
    pairs: list[tuple[str, ctypes.c_uint64, ctypes.c_uint64, int]],
    counter: ctypes.c_uint64,
    grid_dim: int,
) -> list[tuple[str, int]]:
    """Compare each redundant pair on the device; return the non-zero counts."""
    found: list[tuple[str, int]] = []
    host_count = ctypes.c_uint32()
    for label, lhs, rhs, element_count in pairs:
        driver.check(
            driver.lib.cuMemsetD32_v2(ctypes.c_uint64(int(counter.value)), 0, 1),
            "cuMemsetD32_v2(counter)",
        )
        values = (
            ctypes.c_uint64(int(lhs.value)),
            ctypes.c_uint64(int(rhs.value)),
            ctypes.c_uint32(int(element_count)),
            ctypes.c_uint64(int(counter.value)),
        )
        _launch(driver, compare, _launch_params(*values), grid_dim=grid_dim)
        driver.check(driver.lib.cuCtxSynchronize(), f"cuCtxSynchronize(compare {label})")
        driver.check(
            driver.lib.cuMemcpyDtoH_v2(
                ctypes.byref(host_count),
                ctypes.c_uint64(int(counter.value)),
                ctypes.sizeof(host_count),
            ),
            f"cuMemcpyDtoH_v2(compare {label})",
        )
        if int(host_count.value):
            found.append((label, int(host_count.value)))
    return found


class _Buffers:
    """Device allocations for the redundant integer and FP32 streams."""

    names = ("int_x_a", "int_y_a", "int_x_b", "int_y_b", "verify_x", "verify_y", "counter")

    def __init__(self, driver: _CudaDriver) -> None:
        self.driver = driver
        self.pointers: dict[str, ctypes.c_uint64] = {}
        sizes = {name: DEFAULT_STRESS_ELEMENTS * 4 for name in self.names[:4]}
        sizes.update(verify_x=DEFAULT_VERIFY_ELEMENTS * 4, verify_y=DEFAULT_VERIFY_ELEMENTS * 4, counter=4)
        for name in self.names:
            pointer = ctypes.c_uint64()
            driver.check(
                driver.lib.cuMemAlloc_v2(ctypes.byref(pointer), sizes[name]),
                f"cuMemAlloc_v2({name})",
            )
            self.pointers[name] = pointer

    def __getattr__(self, name: str) -> ctypes.c_uint64:
        try:
            return self.__dict__["pointers"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def release(self) -> None:
        for pointer in self.pointers.values():
            if pointer.value:
                try:
                    self.driver.lib.cuMemFree_v2(pointer)
                except Exception:  # noqa: BLE001, S110
                    pass


def run_cuda_bruteforce_test(
    *,
    gpu_index: int,
    duration_seconds: float,
    driver: _CudaDriver | None = None,
    hang_timeout_s: float = HANG_TIMEOUT_S,
    on_hang=_exit_on_hang,
) -> None:
    driver = driver or _CudaDriver()
    context = ctypes.c_void_p()
    module = ctypes.c_void_p()
    functions = {
        name: ctypes.c_void_p() for name in ("brute_force_kernel", "compare_u32_kernel")
    }
    buffers: _Buffers | None = None
    seed0 = 0x13579BDF
    seed1 = 0x2468ACE1
    launches = 0
    compares = 0
    verification_passes = 0
    ptx_buffer = ctypes.create_string_buffer(PTX_SOURCE)
    watchdog = _Watchdog(hang_timeout_s, on_hang)

    driver.check(driver.lib.cuInit(0), "cuInit")
    device = ctypes.c_int()
    driver.check(
        driver.lib.cuDeviceGet(ctypes.byref(device), int(gpu_index)), "cuDeviceGet"
    )
    driver.check(
        driver.lib.cuCtxCreate_v2(ctypes.byref(context), 0, int(device.value)),
        "cuCtxCreate_v2",
    )
    try:
        driver.check(
            driver.lib.cuModuleLoadData(
                ctypes.byref(module), ctypes.cast(ptx_buffer, ctypes.c_void_p)
            ),
            "cuModuleLoadData",
        )
        for name, handle in functions.items():
            driver.check(
                driver.lib.cuModuleGetFunction(ctypes.byref(handle), module, name.encode()),
                f"cuModuleGetFunction({name})",
            )
        brute_force = functions["brute_force_kernel"]
        compare = functions["compare_u32_kernel"]
        buffers = _Buffers(driver)
        schedule = ramp_schedule(float(duration_seconds))

        _log(
            f"starting gpu-index={int(gpu_index)} duration={float(duration_seconds):.1f}s "
            f"stress-elements={DEFAULT_STRESS_ELEMENTS} stress-rounds={DEFAULT_STRESS_ROUNDS} "
            "redundant-copies=2 "
            "ramp=" + "/".join(f"{int(stage.fraction * 100)}%:{stage.seconds:.1f}s" for stage in schedule)
        )

        def sanity_check() -> None:
            _launch_kernel(
                driver, brute_force,
                out_x=buffers.verify_x, out_y=buffers.verify_y,
                element_count=DEFAULT_VERIFY_ELEMENTS, rounds=DEFAULT_VERIFY_ROUNDS,
                seed0=seed0, seed1=seed1, grid_dim=FULL_GRID_BLOCKS,
            )
            driver.check(driver.lib.cuCtxSynchronize(), "cuCtxSynchronize(sanity)")
            _verify_outputs(
                driver, buffers.verify_x, buffers.verify_y,
                element_count=DEFAULT_VERIFY_ELEMENTS, rounds=DEFAULT_VERIFY_ROUNDS,
                seed0=seed0, seed1=seed1, label="sanity",
            )

        watchdog.start()
        sanity_check()
        verification_passes += 1
        watchdog.beat()

        start_monotonic = time.monotonic()
        verify_interval_s = _verify_interval_s(float(duration_seconds))
        next_verify_monotonic = start_monotonic + verify_interval_s
        last_stress_seed0 = seed0
        last_stress_seed1 = seed1
        last_elements = DEFAULT_VERIFY_ELEMENTS
        try:
            for stage in schedule:
                stage_deadline = time.monotonic() + float(stage.seconds)
                grid_dim = stage.grid_blocks
                elements = stage.elements
                _log(
                    f"stage occupancy={int(stage.fraction * 100)}% grid={grid_dim} "
                    f"elements={elements} seconds={stage.seconds:.1f}"
                )
                while time.monotonic() < stage_deadline:
                    for _ in range(DEFAULT_STRESS_BATCH_LAUNCHES):
                        launch_seed0 = _stress_seed(seed0, launches, 0xA5A5A5A5)
                        launch_seed1 = _stress_seed(seed1, launches, 0x5A5A5A5A)
                        # Two copies of the same work; block scheduling spreads
                        # them over different SMs, so one bad SM shows up as a
                        # mismatch instead of hiding between spot checks.
                        for out_x, out_y in ((buffers.int_x_a, buffers.int_y_a), (buffers.int_x_b, buffers.int_y_b)):
                            _launch_kernel(
                                driver, brute_force, out_x=out_x, out_y=out_y,
                                element_count=elements, rounds=DEFAULT_STRESS_ROUNDS,
                                seed0=launch_seed0, seed1=launch_seed1, grid_dim=grid_dim,
                            )
                        last_stress_seed0 = launch_seed0
                        last_stress_seed1 = launch_seed1
                        last_elements = elements
                        launches += 1
                    driver.check(driver.lib.cuCtxSynchronize(), "cuCtxSynchronize(stress)")
                    watchdog.beat()
                    mismatches = _count_mismatches(
                        driver, compare,
                        pairs=[
                            ("int.x", buffers.int_x_a, buffers.int_x_b, elements),
                            ("int.y", buffers.int_y_a, buffers.int_y_b, elements),
                        ],
                        counter=buffers.counter, grid_dim=FULL_GRID_BLOCKS,
                    )
                    compares += 1
                    watchdog.beat()
                    if mismatches:
                        detail = ", ".join(f"{label}={count}" for label, count in mismatches)
                        raise CudaInstabilityError(
                            f"redundant verification mismatch {detail} "
                            f"stage={int(stage.fraction * 100)}% launch={launches}"
                        )
                    now_monotonic = time.monotonic()
                    if now_monotonic >= next_verify_monotonic:
                        _verify_outputs(
                            driver, buffers.int_x_a, buffers.int_y_a,
                            element_count=last_elements, rounds=DEFAULT_STRESS_ROUNDS,
                            seed0=last_stress_seed0, seed1=last_stress_seed1, label="stress",
                        )
                        verification_passes += 1
                        sanity_check()
                        verification_passes += 1
                        watchdog.beat()
                        next_verify_monotonic = now_monotonic + verify_interval_s

            if launches > 0:
                _verify_outputs(
                    driver, buffers.int_x_a, buffers.int_y_a,
                    element_count=last_elements, rounds=DEFAULT_STRESS_ROUNDS,
                    seed0=last_stress_seed0, seed1=last_stress_seed1, label="stress",
                )
                verification_passes += 1
            sanity_check()
            verification_passes += 1
        except CudaInstabilityError:
            raise
        except CudaDriverError as exc:
            # A fault raised by the driver while the GPU is under load is the
            # GPU failing, not the setup failing.
            raise CudaInstabilityError(f"gpu fault under load: {exc}") from exc
        finally:
            watchdog.stop()
        _log(
            f"completed launches={launches} compares={compares} "
            f"verifications={verification_passes} "
            f"elapsed={time.monotonic() - start_monotonic:.1f}s"
        )
    finally:
        watchdog.stop()
        if buffers is not None:
            buffers.release()
        module_value = module.value
        if module_value is not None and int(module_value) != 0:
            try:
                driver.lib.cuModuleUnload(module)
            except Exception:  # noqa: BLE001, S110
                pass
        context_value = context.value
        if context_value is not None and int(context_value) != 0:
            try:
                driver.lib.cuCtxDestroy_v2(context)
            except Exception:  # noqa: BLE001, S110
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a deterministic CUDA brute-force stability workload.",
    )
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--duration-seconds", type=float, default=90.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_cuda_bruteforce_test(
            gpu_index=int(args.gpu_index),
            duration_seconds=float(args.duration_seconds),
        )
    except KeyboardInterrupt:
        _log("Interrupted by user.")
        return 130
    except CudaInstabilityError as exc:
        _log(f"UNSTABLE {exc}")
        return EXIT_INSTABILITY
    except Exception as exc:  # noqa: BLE001
        _log(f"FAILED {exc}")
        return EXIT_SETUP_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
