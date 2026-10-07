from __future__ import annotations

from stability import cuda_bruteforce


def test_cuda_stress_seed_varies_each_launch_and_stays_u32() -> None:
    first = cuda_bruteforce._stress_seed(0x13579BDF, 0, 0xA5A5A5A5)
    second = cuda_bruteforce._stress_seed(0x13579BDF, 1, 0xA5A5A5A5)

    assert first != second
    assert 0 <= first <= 0xFFFFFFFF
    assert 0 <= second <= 0xFFFFFFFF


def test_cuda_verification_samples_cover_edges_and_middle() -> None:
    element_count = cuda_bruteforce.DEFAULT_STRESS_ELEMENTS

    indices = cuda_bruteforce._verification_sample_indices(element_count)

    assert indices[:4] == [0, 1, 2, 7]
    assert element_count // 2 in indices
    assert element_count - 1 in indices
    assert len(indices) == len(set(indices))
    assert all(0 <= index < element_count for index in indices)


def test_cuda_cpu_reference_is_deterministic_for_stress_rounds() -> None:
    seed0 = cuda_bruteforce._stress_seed(0x13579BDF, 17, 0xA5A5A5A5)
    seed1 = cuda_bruteforce._stress_seed(0x2468ACE1, 17, 0x5A5A5A5A)

    first = cuda_bruteforce._cpu_reference(
        4095,
        cuda_bruteforce.DEFAULT_STRESS_ROUNDS,
        seed0,
        seed1,
    )
    second = cuda_bruteforce._cpu_reference(
        4095,
        cuda_bruteforce.DEFAULT_STRESS_ROUNDS,
        seed0,
        seed1,
    )

    assert first == second
    assert all(0 <= value <= 0xFFFFFFFF for value in first)


def test_cuda_verify_interval_scales_for_short_runs_and_caps_long_runs() -> None:
    assert cuda_bruteforce._verify_interval_s(3.0) == 1.0
    assert cuda_bruteforce._verify_interval_s(5.0) == 5.0 / 3.0
    assert cuda_bruteforce._verify_interval_s(150.0) == 5.0


# ---------------------------------------------------------------------------
# Soft-failure design: ramp, redundant compare, watchdog, exit codes.
# ---------------------------------------------------------------------------

import ctypes  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from itertools import pairwise  # noqa: E402

import pytest  # noqa: E402


def test_ramp_schedule_runs_partial_occupancy_before_full_load() -> None:
    stages = cuda_bruteforce.ramp_schedule(5.0)
    assert [s.fraction for s in stages] == [0.125, 0.25, 0.5, 1.0]
    assert [s.seconds for s in stages] == [0.5, 0.5, 0.5, 3.5]
    assert [s.grid_blocks for s in stages] == [32, 64, 128, 256]
    assert [s.elements for s in stages][-1] == cuda_bruteforce.DEFAULT_STRESS_ELEMENTS
    assert all(a.elements < b.elements for a, b in pairwise(stages))
    long = cuda_bruteforce.ramp_schedule(75.0)
    assert [s.seconds for s in long] == [1.0, 1.0, 1.0, 72.0]
    short = cuda_bruteforce.ramp_schedule(1.0)
    assert short[-1].fraction == 1.0
    assert sum(s.seconds for s in short) == pytest.approx(1.0)
    assert all(s.seconds >= cuda_bruteforce.RAMP_STAGE_MIN_S for s in short)


def test_ptx_ships_the_stress_and_device_compare_kernels() -> None:
    ptx = cuda_bruteforce.PTX_SOURCE.decode()
    assert ".entry brute_force_kernel(" in ptx
    assert ".entry compare_u32_kernel(" in ptx
    assert "atom.global.add.u32" in ptx
    assert "fma" not in ptx  # FP32 chains draw less power and weaken the test.


def test_watchdog_fires_once_after_a_stall_and_not_while_beating() -> None:
    now = [100.0]
    hangs: list[float] = []
    dog = cuda_bruteforce._Watchdog(2.0, hangs.append, clock=lambda: now[0], poll_s=0.01)
    dog.start()
    try:
        for _ in range(5):
            now[0] += 1.5
            dog.beat()
            time.sleep(0.03)
        assert hangs == []
        now[0] += 3.0
        deadline = time.monotonic() + 2.0
        while not hangs and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(hangs) == 1 and hangs[0] >= 2.0
    finally:
        dog.stop()


class _Fn:
    """A fake libcuda entry point: accepts argtypes/restype like ctypes does."""

    def __init__(self, handler):
        self.handler = handler

    def __call__(self, *args):
        return int(self.handler(*args))


def _deref(arg):
    return arg._obj  # ctypes.byref(...) keeps the target as _obj


class FakeCudaLib:
    """Enough of the driver API to run the workload loop without a GPU.

    Device memory is a few address ranges; reading a stress element returns
    the CPU reference for the launch that last wrote that buffer, so the
    sanity/stress checks pass unless a scenario says otherwise.
    """

    def __init__(self, *, init_rc=0, sync_rcs=(), mismatch_at_compare=None,
                 mismatch_count=2, sync_sleep_at=None, sync_sleep_s=0.0):
        self.init_rc = init_rc
        self.sync_rcs = dict(sync_rcs)
        self.mismatch_at_compare = mismatch_at_compare
        self.mismatch_count = mismatch_count
        self.sync_sleep_at = sync_sleep_at
        self.sync_sleep_s = sync_sleep_s
        self.allocations: list[tuple[int, int]] = []
        self.launches: list[tuple[str, int, list[int]]] = []
        self.last_write: dict[int, tuple[int, int, int, int]] = {}
        self.memory: dict[int, int] = {}
        self.syncs = 0
        self.compares = 0
        self.functions = {b"brute_force_kernel": 11, b"compare_u32_kernel": 22}
        self.next_address = 0x10000
        for name in (
            "cuInit", "cuDeviceGet", "cuCtxCreate_v2", "cuCtxDestroy_v2",
            "cuModuleLoadData", "cuModuleUnload", "cuModuleGetFunction",
            "cuMemAlloc_v2", "cuMemFree_v2", "cuMemcpyDtoH_v2", "cuMemsetD32_v2",
            "cuLaunchKernel", "cuCtxSynchronize",
        ):
            setattr(self, name, _Fn(getattr(self, f"_{name}")))

    def _cuInit(self, _flags):
        return self.init_rc

    def _cuDeviceGet(self, device, index):
        _deref(device).value = int(index)
        return 0

    def _cuCtxCreate_v2(self, context, _flags, _device):
        _deref(context).value = 1
        return 0

    def _cuCtxDestroy_v2(self, _context):
        return 0

    def _cuModuleLoadData(self, module, _image):
        _deref(module).value = 2
        return 0

    def _cuModuleUnload(self, _module):
        return 0

    def _cuModuleGetFunction(self, handle, _module, name):
        _deref(handle).value = self.functions[name]
        return 0

    def _cuMemAlloc_v2(self, pointer, size):
        address = self.next_address
        self.next_address += int(size) + 0x1000
        self.allocations.append((address, int(size)))
        _deref(pointer).value = address
        return 0

    def _cuMemFree_v2(self, _pointer):
        return 0

    def _cuMemsetD32_v2(self, address, value, _count):
        self.memory[int(address.value)] = int(value)
        return 0

    def _cuLaunchKernel(self, function, gx, _gy, _gz, _bx, _by, _bz, _shm, _stream, params, _extra):
        handle = int(function.value)
        if handle == 11:
            types = (ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
            name = "brute_force"
        else:
            types = (ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint64)
            name = "compare"
        values = [ctypes.cast(params[i], ctypes.POINTER(t)).contents.value for i, t in enumerate(types)]
        self.launches.append((name, int(gx), values))
        if name == "brute_force":
            out_x, out_y, _n, rounds, seed0, seed1 = values
            self.last_write[out_x] = (rounds, seed0, seed1, 0)
            self.last_write[out_y] = (rounds, seed0, seed1, 1)
        else:
            self.compares += 1
            counter = values[3]
            if self.compares == self.mismatch_at_compare:
                self.memory[counter] = self.mismatch_count
        return 0

    def _cuCtxSynchronize(self):
        self.syncs += 1
        if self.syncs == self.sync_sleep_at:
            time.sleep(self.sync_sleep_s)
        return self.sync_rcs.get(self.syncs, 0)

    def _cuMemcpyDtoH_v2(self, host, address, _size):
        address = int(address.value)
        target = _deref(host)
        if address in self.memory:
            target.value = self.memory[address]
            return 0
        for base, size in self.allocations:
            if base <= address < base + size:
                rounds, seed0, seed1, component = self.last_write[base]
                index = (address - base) // 4
                target.value = cuda_bruteforce._cpu_reference(index, rounds, seed0, seed1)[component]
                return 0
        raise AssertionError(f"read from unknown device address {address:#x}")


def _run(lib, **kwargs):
    driver = cuda_bruteforce._CudaDriver(lib=lib)
    return cuda_bruteforce.run_cuda_bruteforce_test(
        gpu_index=0, duration_seconds=1.0, driver=driver, **kwargs
    )


def test_clean_run_ramps_and_compares_both_copies_of_every_launch() -> None:
    lib = FakeCudaLib()
    _run(lib, on_hang=lambda _s: pytest.fail("no hang expected"))
    verify_elements = cuda_bruteforce.DEFAULT_VERIFY_ELEMENTS
    stress = [(gx, v) for name, gx, v in lib.launches if name == "brute_force" and v[2] > verify_elements]
    sanity = [gx for name, gx, v in lib.launches if name == "brute_force" and v[2] == verify_elements]
    assert sanity and set(sanity) == {256}  # CPU-checked sanity runs stay at full grid
    expected = cuda_bruteforce.ramp_schedule(1.0)
    stress_grids = [gx for gx, _ in stress]
    assert sorted(set(stress_grids)) == [s.grid_blocks for s in expected]
    assert stress_grids == sorted(stress_grids)  # ramp never steps back down
    assert {v[2] for gx, v in stress} == {s.elements for s in expected}
    # Same seeds land in two different buffers, back to back.
    for (_, first), (_, second) in zip(stress[0::2], stress[1::2]):
        assert first[3:] == second[3:] and first[0] != second[0] and first[1] != second[1]
    compares = [v for name, _, v in lib.launches if name == "compare"]
    batches = len(stress) // (2 * cuda_bruteforce.DEFAULT_STRESS_BATCH_LAUNCHES)
    assert batches >= 3 and len(compares) == 2 * batches  # int.x and int.y per batch
    assert all(v[2] in {s.elements for s in expected} for v in compares)


def test_device_mismatch_is_instability_not_a_setup_error() -> None:
    lib = FakeCudaLib(mismatch_at_compare=3, mismatch_count=5)
    with pytest.raises(cuda_bruteforce.CudaInstabilityError, match="verification mismatch int.x=5"):
        _run(lib)
    assert lib.compares == 4  # int.x flagged, int.y still compared, then raised


def test_gpu_fault_under_load_is_instability() -> None:
    lib = FakeCudaLib(sync_rcs={4: 700})
    with pytest.raises(cuda_bruteforce.CudaInstabilityError, match="gpu fault under load: cuCtxSynchronize"):
        _run(lib)


def test_setup_failure_is_not_instability() -> None:
    with pytest.raises(cuda_bruteforce.CudaDriverError, match="cuInit failed rc=100") as info:
        _run(FakeCudaLib(init_rc=100))
    assert not isinstance(info.value, cuda_bruteforce.CudaInstabilityError)


def test_stalled_synchronize_triggers_the_hang_handler_while_blocked() -> None:
    hangs: list[float] = []
    seen = threading.Event()

    def on_hang(stalled_s):
        hangs.append(stalled_s)
        seen.set()

    lib = FakeCudaLib(sync_sleep_at=3, sync_sleep_s=0.8)
    _run(lib, hang_timeout_s=0.2, on_hang=on_hang)
    assert seen.is_set() and hangs and hangs[0] >= 0.2


def test_main_maps_exceptions_to_the_documented_exit_codes(monkeypatch) -> None:
    def failing(exc):
        def run(**_kwargs):
            raise exc
        return run

    monkeypatch.setattr(cuda_bruteforce, "run_cuda_bruteforce_test",
                        failing(cuda_bruteforce.CudaInstabilityError("redundant verification mismatch int.x=1")))
    assert cuda_bruteforce.main(["--duration-seconds", "1"]) == cuda_bruteforce.EXIT_INSTABILITY == 3
    monkeypatch.setattr(cuda_bruteforce, "run_cuda_bruteforce_test",
                        failing(cuda_bruteforce.CudaDriverError("cuInit failed rc=100")))
    assert cuda_bruteforce.main(["--duration-seconds", "1"]) == cuda_bruteforce.EXIT_SETUP_ERROR == 1
    monkeypatch.setattr(cuda_bruteforce, "run_cuda_bruteforce_test", lambda **_k: None)
    assert cuda_bruteforce.main(["--duration-seconds", "1"]) == 0
    assert {3, 4} == cuda_bruteforce.CUDA_INSTABILITY_EXIT_CODES
