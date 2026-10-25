// Observe supported NR producer interfaces without replacing the model DLL.
// Each adapter forwards through the original producer, preserving the model's
// legitimate caller. All adapters share GPU timing and telemetry.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "nr_observer.h"
#include "nr_forwarder.h"
#include "../third_party/minhook/include/MinHook.h"

#include <cstdio>

namespace {
using Create = int (__cdecl*)(ID3D12GraphicsCommandList*, int, void*, void**);
using Evaluate = int (__cdecl*)(ID3D12GraphicsCommandList*, const void*, const void*, void*);
using Release = int (__cdecl*)(void*);
Create g_create = nullptr;
Evaluate g_evaluate = nullptr;
Release g_release = nullptr;
NrEmit g_emit = nullptr;
NrFeatures g_features;
bool succeeded(int result) { return (unsigned(result) & 0xfff00000u) != 0xbad00000u; }

int __cdecl create(ID3D12GraphicsCommandList* commands, int feature, void* params, void** out) {
    const int result = g_create(commands, feature, params, out);
    if (feature == 18 && succeeded(result) && out && *out) {
        g_features.created(commands, *out);
    }
    return result;
}

int __cdecl evaluate(ID3D12GraphicsCommandList* commands, const void* feature, const void* params, void* callback) {
    NrEvaluation timing(commands, g_features.contains(feature));
    const int result = g_evaluate(commands, feature, params, callback);
    timing.finish(succeeded(result));
    return result;
}

int __cdecl release(void* feature) {
    const int result = g_release(feature);
    if (succeeded(result)) {
        g_features.released(feature);
    }
    return result;
}

bool install(HMODULE module) {
    NrHook hooks[] = {
        {"NVSDK_NGX_D3D12_CreateFeature", reinterpret_cast<void*>(&create), reinterpret_cast<void**>(&g_create), nullptr},
        {"NVSDK_NGX_D3D12_EvaluateFeature", reinterpret_cast<void*>(&evaluate), reinterpret_cast<void**>(&g_evaluate), nullptr},
        {"NVSDK_NGX_D3D12_ReleaseFeature", reinterpret_cast<void*>(&release), reinterpret_cast<void**>(&g_release), nullptr},
    };
    return nr_install_hooks(module, hooks, 3);
}

DWORD WINAPI worker(LPVOID) {
    if (MH_Initialize() != MH_OK) return 0;
    struct Producer {
        const char* module_name;
        const char* name;
        bool (*install_hooks)(HMODULE);
        HMODULE module = nullptr;
    };
    Producer producers[] = {
        {"_nvngx.dll", "NGX D3D12 feature-18", install},
        {"nvngx.dll_dlssnr.dll", "NR D3D12 forwarder ABI v2", nr_forwarder::install},
    };

    for (;;) {
        // Independent discovery: absence or failure of one producer must not
        // prevent another from being observed. Keep references and trampolines
        // for the process lifetime. Never install hooks under DllMain.
        for (auto& producer : producers) {
            if (producer.module || !GetModuleHandleExA(0, producer.module_name, &producer.module)) continue;
            const bool ready = producer.install_hooks(producer.module);
            char message[192];
            const int length = std::snprintf(message, sizeof(message),
                "[pb-nr-gpu] %s observer %s\n", producer.name,
                ready ? "ready" : "unavailable; original runtime retained");
            if (length > 0 && length < int(sizeof(message))) g_emit(message, length);
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
