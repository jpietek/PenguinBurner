// Observe NGX feature 18 at the driver dispatcher. The driver's own call into
// the NVIDIA model retains its legitimate caller, including caller validation.
// No model DLL replacement, private struct offsets, or shader-name detection.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "nr_gpu.h"
#include "../third_party/minhook/include/MinHook.h"

#include <cstdio>
#include <mutex>
#include <unordered_set>

namespace {
using Create = int (__cdecl*)(ID3D12GraphicsCommandList*, int, void*, void**);
using Evaluate = int (__cdecl*)(ID3D12GraphicsCommandList*, const void*, const void*, void*);
using Release = int (__cdecl*)(void*);
Create g_create = nullptr;
Evaluate g_evaluate = nullptr;
Release g_release = nullptr;
NrEmit g_emit = nullptr;
std::mutex g_features_mutex;
std::unordered_set<const void*> g_features;
bool succeeded(int result) { return (unsigned(result) & 0xfff00000u) != 0xbad00000u; }

int __cdecl create(ID3D12GraphicsCommandList* commands, int feature, void* params, void** out) {
    const int result = g_create(commands, feature, params, out);
    if (feature == 18 && succeeded(result) && out && *out) {
        std::lock_guard lock(g_features_mutex);
        if (g_features.size() < 1024) g_features.insert(*out);
    }
    return result;
}

int __cdecl evaluate(ID3D12GraphicsCommandList* commands, const void* feature, const void* params, void* callback) {
    bool nr = false;
    {
        std::lock_guard lock(g_features_mutex);
        nr = g_features.count(feature) != 0;
    }
    const auto ticket = nr ? nr_gpu_begin(commands) : NrGpuTicket{};
    const int result = g_evaluate(commands, feature, params, callback);
    nr_gpu_end(ticket, succeeded(result));
    return result;
}

int __cdecl release(void* feature) {
    const int result = g_release(feature);
    if (succeeded(result)) {
        std::lock_guard lock(g_features_mutex);
        g_features.erase(feature);
    }
    return result;
}

using CreateDevice = HRESULT (WINAPI*)(IUnknown*, D3D_FEATURE_LEVEL, REFIID, void**);
CreateDevice g_create_device = nullptr;
HRESULT WINAPI create_device(IUnknown* adapter, D3D_FEATURE_LEVEL level, REFIID riid, void** out) {
    const HRESULT result = g_create_device(adapter, level, riid, out);
    if (SUCCEEDED(result) && out && *out) {
        ID3D12Device* device = nullptr;
        if (SUCCEEDED(static_cast<IUnknown*>(*out)->QueryInterface(IID_PPV_ARGS(&device)))) {
            nr_gpu_observe_device(device);
            device->Release();
        }
    }
    return result;
}

bool install_device_observer(HMODULE module) {
    auto address = reinterpret_cast<void*>(GetProcAddress(module, "D3D12CreateDevice"));
    if (!address || MH_CreateHook(address, reinterpret_cast<void*>(&create_device),
        reinterpret_cast<void**>(&g_create_device)) != MH_OK) return false;
    return MH_EnableHook(address) == MH_OK;
}

bool install(HMODULE module) {
    struct Hook { const char* name; void* replacement; void** original; void* address; };
    Hook hooks[] = {
        {"NVSDK_NGX_D3D12_CreateFeature", reinterpret_cast<void*>(&create), reinterpret_cast<void**>(&g_create), nullptr},
        {"NVSDK_NGX_D3D12_EvaluateFeature", reinterpret_cast<void*>(&evaluate), reinterpret_cast<void**>(&g_evaluate), nullptr},
        {"NVSDK_NGX_D3D12_ReleaseFeature", reinterpret_cast<void*>(&release), reinterpret_cast<void**>(&g_release), nullptr},
    };
    unsigned created = 0;
    for (auto& hook : hooks) {
        hook.address = reinterpret_cast<void*>(GetProcAddress(module, hook.name));
        if (!hook.address || MH_CreateHook(hook.address, hook.replacement, hook.original) != MH_OK) break;
        ++created;
    }
    if (created != 3) {
        for (unsigned i = 0; i < created; ++i) MH_RemoveHook(hooks[i].address);
        return false;
    }
    for (auto& hook : hooks) MH_QueueEnableHook(hook.address);
    if (MH_ApplyQueued() != MH_OK) return false;
    const char message[] = "[pb-nr-gpu] NGX D3D12 feature-18 observer ready\n";
    g_emit(message, sizeof(message) - 1);
    return true;
}

DWORD WINAPI worker(LPVOID) {
    if (MH_Initialize() != MH_OK) return 0;
    HMODULE driver = nullptr;
    // Acquire a reference rather than loading NGX ourselves. Retain it while
    // its intercepted entry points are in use. Never run MinHook in DllMain.
    while (!GetModuleHandleExA(0, "_nvngx.dll", &driver)) Sleep(25);
    if (!install(driver)) {
        // A failed batch can have enabled an earlier hook. Retain the module
        // and all valid trampolines so forwarding remains safe in that case.
        const char message[] = "[pb-nr-gpu] NGX observer unavailable; original runtime retained\n";
        g_emit(message, sizeof(message) - 1);
        return 0;
    }
    HMODULE d3d12 = nullptr;
    bool device_observer_attempted = false;
    for (;;) {
        if (!device_observer_attempted && GetModuleHandleExA(0, "d3d12.dll", &d3d12)) {
            device_observer_attempted = true;
            if (install_device_observer(d3d12)) {
                const char message[] = "[pb-nr-gpu] observing application D3D12 queues\n";
                g_emit(message, sizeof(message) - 1);
            }
        }
        nr_gpu_poll(g_emit);
        Sleep(10);
    }
}
} // namespace

void start_nr_telemetry(NrEmit emit) {
    g_emit = emit;
    HMODULE self = nullptr;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           reinterpret_cast<LPCSTR>(&worker), &self)) return;
    HANDLE thread = CreateThread(nullptr, 0, worker, nullptr, 0, nullptr);
    if (thread) CloseHandle(thread);
}
