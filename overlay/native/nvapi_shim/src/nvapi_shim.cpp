// PenguinBurner NVAPI latency shim — a drop-in proxy nvapi64.dll.
//
// nvapi64.dll exposes exactly one export, `nvapi_QueryInterface(id)`, which the
// app uses to look up every NVAPI function by a 32-bit id. This DLL re-exports
// only that symbol, forwards every call to the *real* (dxvk-nvapi) nvapi64.dll,
// and swaps in its own wrappers for the Reflex latency-marker functions so we
// can emit the same per-frame marker stream the dxvk-nvapi trace produced — at
// marker rate instead of full trace, with NO fork of dxvk-nvapi and NO
// per-Proton rebuild. The launcher fronts system32\nvapi64.dll with this proxy
// and parks the real dxvk-nvapi next to us as nvapi64-pb.dll (kept refreshed
// across Proton re-syncs), which is what we forward to.
//
// Markers under frame generation are dropped at vkd3d's owner-gate before they
// reach Vulkan, so a Vulkan layer can't see them; tapping here at the NVAPI ABI
// is *above* the owner-gate, so they survive. NVAPI is a stable public ABI, so
// the ids/struct offsets below never change across dxvk-nvapi/Proton versions.
//
// Output is written to stderr in the exact format
// overlay/telemetry/nvapi_marker_bridge.py already parses (`_MARKER_LOG_RE`), so
// nothing downstream changes — the launcher already dup2's the game's stderr
// onto the marker FIFO the bridge drains.
//
// ABI constants verified against dxvk-nvapi external/nvapi/{nvapi.h,
// nvapi_interface.h}: see NV_LATENCY_MARKER_PARAMS_V1 (size 88; version@0,
// frameID@8, markerType@16) and the id table.

#define WIN32_LEAN_AND_MEAN
// Older MinGW (e.g. EPEL8's 7.2 in the manylinux wheel container) defaults
// _WIN32_WINNT below Vista, hiding InitOnceExecuteOnce. Everything this shim
// runs under (Proton/Wine, Streamline titles) is far newer than Win7.
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>
#include "nr_telemetry.h"
#include "reflex_report.h"

namespace {

// ---- minimal NVAPI ABI (exact layouts from dxvk-nvapi external/nvapi/nvapi.h)
using NvU32 = unsigned int;
using NvU64 = unsigned long long;
using NvU8 = unsigned char;
using NvAPI_Status = int;             // NVAPI_OK == 0
using NV_LATENCY_MARKER_TYPE = int;   // enum, 4 bytes

// Function ids (nvapi_interface.h).
constexpr NvU32 kIdSetSleepMode = 0xac1ca9e0;
constexpr NvU32 kIdGetLatency = 0x1a587f9c;
constexpr NvU32 kIdSetLatencyMarker = 0xd9984c05;
constexpr NvU32 kIdAsyncFrameMarker = 0x13c98f73;

// NV_LATENCY_MARKER_PARAMS_V1 — size 88.
struct NvLatencyMarkerParams {
    NvU32 version;                  // @0
    NvU64 frameID;                  // @8
    NV_LATENCY_MARKER_TYPE markerType;  // @16
    NvU64 rsvd0;                    // @24
    NvU8 rsvd[56];                  // @32
};

// NV_ASYNC_FRAME_MARKER_PARAMS_V1 (D3D12_SetAsyncFrameMarker, FG present path).
struct NvAsyncFrameMarkerParams {
    NvU32 version;                  // @0
    NvU64 frameID;                  // @8
    NV_LATENCY_MARKER_TYPE markerType;  // @16
    NvU64 presentFrameID;           // @24
    NvU8 vendorInternal;            // @32
    NvU8 rsvd[55];                  // @33
};

// Bytes we read from a caller marker struct (version@0, frameID@8, markerType@16
// — last byte @19). Used to bound the defensive readability probe below.
constexpr SIZE_T kMarkerReadBytes = 24;

using PfnQueryInterface = void*(__cdecl*)(NvU32 id);
using PfnSetLatencyMarker = NvAPI_Status(__cdecl*)(void* dev, NvLatencyMarkerParams* p);
using PfnAsyncFrameMarker = NvAPI_Status(__cdecl*)(void* queue, NvAsyncFrameMarkerParams* p);
using PfnGetLatency = NvAPI_Status(__cdecl*)(void* dev, void* p);
using PfnSetSleepMode = NvAPI_Status(__cdecl*)(void* dev, void* p);

// ---- shared state ------------------------------------------------------------
HMODULE g_self = nullptr;
HMODULE g_real = nullptr;
PfnQueryInterface g_real_qi = nullptr;
volatile PfnSetLatencyMarker g_real_set_marker = nullptr;
volatile PfnAsyncFrameMarker g_real_async_marker = nullptr;
volatile PfnGetLatency g_real_get_latency = nullptr;
volatile PfnSetSleepMode g_real_set_sleep = nullptr;
INIT_ONCE g_init_once = INIT_ONCE_STATIC_INIT;
CRITICAL_SECTION g_emit_lock;
double g_qpc_to_us = 0.0;
int g_out_fd = -1;
DWORD g_pid = 0;

// Only the marker types the bridge consumes; others are dropped to keep volume
// low (the bridge maps unknown names to None anyway).
const char* marker_name(NV_LATENCY_MARKER_TYPE type) {
    switch (type) {
        case 0: return "SIMULATION_START";
        case 5: return "PRESENT_END";
        case 6: return "INPUT_SAMPLE";
        case 11: return "OUT_OF_BAND_PRESENT_START";
        case 12: return "OUT_OF_BAND_PRESENT_END";
        default: return nullptr;
    }
}

NvU64 qpc_us() {
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    return static_cast<NvU64>(static_cast<double>(counter.QuadPart) * g_qpc_to_us);
}

// ---- non-blocking emit: ring buffer + dedicated writer thread -----------------
//
// emit_raw must NEVER block a game thread. g_out_fd is normally the marker FIFO,
// and a FIFO whose drainer died fills at 64 KB -- after that a blocking _write
// would stall the caller *inside the game's frame loop* (Streamline invokes the
// marker wrappers on the simulation thread) and freeze the game. So game
// threads only copy the line into a fixed ring under the lock; the writer
// thread below does the (possibly blocking) _write on its own time. When the
// output backs up the ring fills and new lines are dropped -- lost latency
// samples beat a frozen game -- and a diagnostic line reports the drop count
// once the output drains again.
constexpr int kRingSlots = 256;  // ~3.4 s of markers at ~75 lines/s
constexpr int kSlotBytes = pb_reflex::kLineBytes;
char g_ring[kRingSlots][kSlotBytes];
int g_ring_len[kRingSlots];
int g_ring_head = 0;  // producer: next slot to fill
int g_ring_tail = 0;  // consumer: next slot to drain
unsigned long long g_dropped = 0;
unsigned long long g_dropped_reported = 0;
HANDLE g_wake = nullptr;           // auto-reset: ring went non-empty
HANDLE g_writer_thread = nullptr;  // null => fall back to direct writes

void emit_raw(const char* buf, int len) {
    if (g_out_fd < 0 || len <= 0) {
        return;
    }
    if (len >= kSlotBytes) {
        len = kSlotBytes - 1;  // defensive cap; real lines never get here
    }
    if (g_writer_thread == nullptr) {
        // Writer thread could not start (init failure): direct write. This is
        // the pre-ring behavior and can block on a stalled pipe; it is only a
        // last resort so a broken init still yields markers.
        EnterCriticalSection(&g_emit_lock);
        _write(g_out_fd, buf, static_cast<unsigned int>(len));
        LeaveCriticalSection(&g_emit_lock);
        return;
    }
    bool queued = false;
    EnterCriticalSection(&g_emit_lock);
    int next = (g_ring_head + 1) % kRingSlots;
    if (next != g_ring_tail) {
        std::memcpy(g_ring[g_ring_head], buf, static_cast<size_t>(len));
        g_ring_len[g_ring_head] = len;
        g_ring_head = next;
        queued = true;
    } else {
        ++g_dropped;  // output stalled; drop, never block the game
    }
    LeaveCriticalSection(&g_emit_lock);
    if (queued && g_wake != nullptr) {
        SetEvent(g_wake);
    }
}

DWORD WINAPI writer_main(LPVOID) {
    char local[kSlotBytes];
    for (;;) {
        // Timeout so drop reporting still happens if a wake was coalesced away.
        WaitForSingleObject(g_wake, 1000);
        for (;;) {
            int len = 0;
            unsigned long long dropped = 0;
            EnterCriticalSection(&g_emit_lock);
            if (g_ring_tail != g_ring_head) {
                len = g_ring_len[g_ring_tail];
                std::memcpy(local, g_ring[g_ring_tail], static_cast<size_t>(len));
                g_ring_tail = (g_ring_tail + 1) % kRingSlots;
            } else {
                dropped = g_dropped;
            }
            LeaveCriticalSection(&g_emit_lock);
            if (len > 0) {
                // May block on a full pipe -- that parks only this thread; the
                // ring keeps absorbing (then dropping) game-thread emits.
                _write(g_out_fd, local, static_cast<unsigned int>(len));
                continue;
            }
            if (dropped > g_dropped_reported) {
                int n = std::snprintf(
                    local, sizeof(local),
                    "[pb-nvapi-shim] output stalled: dropped %llu marker lines\n",
                    dropped - g_dropped_reported);
                if (n > 0) {
                    if (n >= static_cast<int>(sizeof(local))) {
                        n = static_cast<int>(sizeof(local)) - 1;
                    }
                    _write(g_out_fd, local, static_cast<unsigned int>(n));
                }
                g_dropped_reported = dropped;
            }
            break;
        }
    }
    return 0;
}

void start_writer_thread() {
    // A caller may query NVAPI during DLL_PROCESS_ATTACH and immediately
    // FreeLibrary its probe handle (OptiScaler does this). The new thread
    // cannot run until the loader lock is released, so it could otherwise
    // start after our code and globals have already been unmapped. The writer
    // lives until process exit; keep its DLL alive for that same lifetime.
    HMODULE pinned = nullptr;
    if (!GetModuleHandleExA(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
            reinterpret_cast<LPCSTR>(&writer_main), &pinned)) {
        return;  // No background thread may outlive an unpinned module.
    }
    g_wake = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    if (g_wake == nullptr) {
        return;  // emit_raw falls back to direct writes
    }
    g_writer_thread = CreateThread(nullptr, 64 * 1024, writer_main, nullptr, 0, nullptr);
    if (g_writer_thread == nullptr) {
        CloseHandle(g_wake);
        g_wake = nullptr;
    }
}

void emit_marker(NvU64 frame_id, NV_LATENCY_MARKER_TYPE type) {
    const char* name = marker_name(type);
    if (!name) {
        return;
    }
    char buf[192];
    int len = std::snprintf(
        buf, sizeof(buf),
        "0.0:%x:0:latency-marker:pb:qpcUs=%llu frameID=%llu markerType=%s\n",
        static_cast<unsigned>(g_pid),
        static_cast<unsigned long long>(qpc_us()),
        static_cast<unsigned long long>(frame_id), name);
    if (len < 0) {
        return;  // formatting error
    }
    // snprintf returns the would-be length; clamp to what's actually in buf so a
    // truncation can never make emit_raw read past the buffer.
    if (len >= static_cast<int>(sizeof(buf))) {
        len = static_cast<int>(sizeof(buf)) - 1;
    }
    emit_raw(buf, len);
}

// ---- init: open output + locate/self-refresh/load the real nvapi64.dll -------
void open_output() {
    const char* path = std::getenv("PENGUIN_BURNER_SHIM_OUTPUT");
    if (path && path[0]) {
        int fd = _open(
            path, _O_WRONLY | _O_CREAT | _O_APPEND | _O_BINARY,
            _S_IREAD | _S_IWRITE);
        if (fd != -1) {
            g_out_fd = fd;
            return;
        }
    }
    // Write at the msvcrt fd level (fd 2). The launcher routes the game's
    // stderr (fd 2) to the marker FIFO the bridge drains, and dxvk-nvapi's own
    // logger reaches it the same way (std::cerr -> stdio -> msvcrt _write(2)).
    // We bypass the FILE* layer: in a *loaded DLL*, mingw's FILE* stderr is not
    // reliably wired to fd 2 (GetStdHandle(STD_ERROR_HANDLE) is also NULL in a
    // GUI game like Talos2-Win64-Shipping.exe), so earlier handle/FILE writes
    // silently produced no syscall. The raw fd does.
    g_out_fd = 2;
}

// We are system32\nvapi64.dll. The launcher parked the real dxvk-nvapi next to
// us under a distinct basename (nvapi64-pb.dll) so we can load it without the
// loader handing us back our own module on a same-name load. The launcher also
// keeps that sidecar refreshed across Proton re-syncs, so we just load it.
bool load_real() {
    char sidecar[MAX_PATH];
    const char* override_path = std::getenv("PENGUIN_BURNER_SHIM_REAL");
    if (override_path && override_path[0]) {
        g_real = LoadLibraryA(override_path);
    }
    if (!g_real) {
        char sysdir[MAX_PATH];
        UINT n = GetSystemDirectoryA(sysdir, MAX_PATH);
        if (n == 0 || n >= MAX_PATH) {
            return false;
        }
        std::snprintf(sidecar, sizeof(sidecar), "%s\\nvapi64-pb.dll", sysdir);
        g_real = LoadLibraryA(sidecar);
    }
    if (!g_real) {
        return false;
    }
    g_real_qi = reinterpret_cast<PfnQueryInterface>(
        GetProcAddress(g_real, "nvapi_QueryInterface"));
    return g_real_qi != nullptr;
}

BOOL CALLBACK init_once(PINIT_ONCE, PVOID, PVOID*) {
    LARGE_INTEGER freq;
    freq.QuadPart = 0;
    QueryPerformanceFrequency(&freq);
    // Guard div-by-zero on a weird/absent timer (would otherwise be inf). A 0
    // scale just yields qpcUs=0 markers (no latency, but never a crash).
    g_qpc_to_us = freq.QuadPart > 0
        ? 1000000.0 / static_cast<double>(freq.QuadPart)
        : 0.0;
    g_pid = GetCurrentProcessId();
    open_output();
    start_writer_thread();
    const char* nr_timing = std::getenv("PENGUIN_BURNER_NR_TIMING");
    if (nr_timing && std::strcmp(nr_timing, "1") == 0) start_nr_telemetry(emit_raw);
    bool ok = load_real();
    char banner[256];
    int len = std::snprintf(
        banner, sizeof(banner),
        "[pb-nvapi-shim] init pid=%u real=%s qi=%p\n",
        static_cast<unsigned>(g_pid), ok ? "ok" : "MISSING",
        reinterpret_cast<void*>(g_real_qi));
    if (len > 0) {
        if (len >= static_cast<int>(sizeof(banner))) {
            len = static_cast<int>(sizeof(banner)) - 1;
        }
        emit_raw(banner, len);
    }
    return TRUE;
}

void ensure_init() {
    InitOnceExecuteOnce(&g_init_once, init_once, nullptr, nullptr);
}

// ---- tapped wrappers (addresses handed to the app via QueryInterface) --------
//
// Defensive contract for ALL wrappers (the shim runs inside the game process, so
// it must never be the cause of a crash or memory corruption):
//   * The ONLY caller memory we touch is a read of a few fixed fields, and only
//     after a null + readability check; if the struct is unexpected we just skip
//     the tap. (A wild pointer would fault in the real nvapi too, so we add no
//     new crash surface — but we make sure it isn't *us* that faults.)
//   * We never write to caller/external memory — emit only formats into a local
//     bounded buffer (snprintf, capped) and writes to our own fd. No corruption.
//   * Every forward target is null-guarded; if the real is missing we return a
//     benign status instead of dereferencing null.
//   * We never block the caller: emits go through the ring buffer above, and a
//     stalled output pipe drops lines instead of stalling the frame loop.
void poll_latency_report(void* dev, ULONGLONG now);

NvAPI_Status __cdecl wrap_set_latency_marker(void* dev, NvLatencyMarkerParams* p) {
    PfnSetLatencyMarker real = g_real_set_marker;
    if (p && !IsBadReadPtr(p, kMarkerReadBytes)) {
        emit_marker(p->frameID, p->markerType);
    }
    const auto status = real ? real(dev, p) : 0;
    if (real && status == 0 && p && !IsBadReadPtr(p, kMarkerReadBytes)
        && p->version == (sizeof(NvLatencyMarkerParams) | (1u << 16))
        && p->markerType == 5) poll_latency_report(dev, GetTickCount64());
    return status;
}

NvAPI_Status __cdecl wrap_async_frame_marker(void* queue, NvAsyncFrameMarkerParams* p) {
    PfnAsyncFrameMarker real = g_real_async_marker;
    if (p && !IsBadReadPtr(p, kMarkerReadBytes)) {
        emit_marker(p->frameID, p->markerType);
    }
    return real ? real(queue, p) : 0;
}

void emit_latency_report(void* dev, void* p, NvAPI_Status status) {
    // Observe only successful V1 results after the driver has filled them.
    // Unknown versions, failures and empty warm-up histories remain untouched.
    if (status == 0 && p && !IsBadReadPtr(p, sizeof(uint32_t))
        && *static_cast<const uint32_t*>(p) == pb_reflex::kVersion
        && !IsBadReadPtr(p, sizeof(pb_reflex::LatencyResults))) {
        char line[pb_reflex::kLineBytes];
        const int length = pb_reflex::format_latest(
            *static_cast<const pb_reflex::LatencyResults*>(p), g_pid,
            reinterpret_cast<uintptr_t>(dev), line, sizeof(line));
        if (length > 0) emit_raw(line, length);
    }
}

NvAPI_Status __cdecl wrap_get_latency(void* dev, void* p) {
    PfnGetLatency real = g_real_get_latency;
    if (!real) return 0;
    const NvAPI_Status status = real(dev, p);
    emit_latency_report(dev, p, status);
    return status;
}

std::atomic<ULONGLONG> g_next_report_poll{0};
void poll_latency_report(void* dev, ULONGLONG now) {
    // Some games submit Reflex markers but never request the report history
    // during frame generation. Query at most ten times per second, on the
    // existing device call thread so no device pointer outlives its caller.
    static thread_local bool polling = false;
    if (!dev || polling) return;
    auto next = g_next_report_poll.load(std::memory_order_relaxed);
    if (now < next || !g_next_report_poll.compare_exchange_strong(next, now + 100,
            std::memory_order_relaxed)) return;
    PfnGetLatency real = g_real_get_latency;
    if (!real && g_real_qi) real = reinterpret_cast<PfnGetLatency>(g_real_qi(kIdGetLatency));
    if (!real) return;
    polling = true;
    pb_reflex::LatencyResults report{};
    report.version = pb_reflex::kVersion;
    const auto status = real(dev, &report);
    emit_latency_report(dev, &report, status);
    polling = false;
}

NvAPI_Status __cdecl wrap_set_sleep_mode(void* dev, void* p) {
    PfnSetSleepMode real = g_real_set_sleep;
    return real ? real(dev, p) : 0;
}

}  // namespace

// ---- the only export: forward everything, wrap the latency ids ---------------
extern "C" void* __cdecl nvapi_QueryInterface(NvU32 id) {
    ensure_init();
    if (!g_real_qi) {
        return nullptr;
    }
    void* real = g_real_qi(id);
    if (!real) {
        return real;
    }
    switch (id) {
        case kIdSetLatencyMarker:
            g_real_set_marker = reinterpret_cast<PfnSetLatencyMarker>(real);
            return reinterpret_cast<void*>(&wrap_set_latency_marker);
        case kIdAsyncFrameMarker:
            g_real_async_marker = reinterpret_cast<PfnAsyncFrameMarker>(real);
            return reinterpret_cast<void*>(&wrap_async_frame_marker);
        case kIdGetLatency:
            g_real_get_latency = reinterpret_cast<PfnGetLatency>(real);
            return reinterpret_cast<void*>(&wrap_get_latency);
        case kIdSetSleepMode:
            g_real_set_sleep = reinterpret_cast<PfnSetSleepMode>(real);
            return reinterpret_cast<void*>(&wrap_set_sleep_mode);
        default:
            return real;
    }
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = inst;
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_emit_lock);
    }
    return TRUE;
}
