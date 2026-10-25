#include <windows.h>
#include <d3d12.h>
#include <type_traits>
#include "../../overlay/native/nvapi_shim/src/nr_forwarder.h"
extern "C" int NVSDK_NGX_D3D12_EvaluateFeature(ID3D12GraphicsCommandList*, const void*, const void*, void*);

extern "C" {
__declspec(dllexport) int dlssnr_abi_version = 2;
__declspec(dllexport) void* dlssnr_call_create(const wchar_t*, const wchar_t*, ID3D12Device*,
    ID3D12GraphicsCommandList* commands, void*, unsigned w, unsigned h, int preset, float intensity,
    int style, float structure, float tone, float skin, int mask, int ui) {
    if (w != 640 || h != 480 || preset != 2 || intensity != .5f || style != 1 ||
        structure != .25f || tone != .75f || skin != 1.f || mask != 1 || ui != 0) return nullptr;
    if (GetEnvironmentVariableA("PB_TEST_NR_NESTED", nullptr, 0)) {
        using Create = int(__cdecl*)(ID3D12GraphicsCommandList*, int, void*, void**);
        auto create = reinterpret_cast<Create>(GetProcAddress(GetModuleHandleA("_nvngx.dll"), "NVSDK_NGX_D3D12_CreateFeature"));
        void* handle = nullptr;
        create(commands, 18, nullptr, &handle);
        return handle;
    }
    return reinterpret_cast<void*>(18);
}
__declspec(dllexport) int dlssnr_call_evaluate(ID3D12GraphicsCommandList* list, void* handle,
    void* params, ID3D12Resource* c, ID3D12Resource* d, ID3D12Resource* m, ID3D12Resource* o,
    unsigned w, unsigned h, unsigned gw, unsigned gh, unsigned mw, unsigned mh,
    unsigned dx, unsigned dy, unsigned mx, unsigned my, int inverted, int reset,
    float intensity, int style, float structure, float tone, float skin, int mask,
    float mvx, float mvy) {
    if (handle != reinterpret_cast<void*>(18) || c || d || m || o || w != 640 || h != 480 ||
        gw != 320 || gh != 240 || mw != 160 || mh != 120 || dx != 2 || dy != 3 ||
        mx != 4 || my != 5 || inverted != 1 || reset != 0 || intensity != .5f || style != 1 ||
        structure != .25f || tone != .75f || skin != 1.f || mask != 1 || mvx != -1.f || mvy != 2.f)
        return 0; // Refusal is not NGX success and must not produce timing.
    if (GetEnvironmentVariableA("PB_TEST_NR_REFUSED", nullptr, 0)) return 0;
    if (GetEnvironmentVariableA("PB_TEST_NR_NESTED", nullptr, 0)) {
        using Eval = decltype(&NVSDK_NGX_D3D12_EvaluateFeature);
        auto evaluate = reinterpret_cast<Eval>(GetProcAddress(GetModuleHandleA("_nvngx.dll"), "NVSDK_NGX_D3D12_EvaluateFeature"));
        return evaluate(list, handle, params, nullptr);
    }
    return NVSDK_NGX_D3D12_EvaluateFeature(list, handle, params, nullptr);
}
void* volatile released_handle = nullptr;
__declspec(dllexport) void dlssnr_call_release(void* handle) { released_handle = handle; }
}
BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH && GetEnvironmentVariableA("PB_TEST_NR_UNKNOWN_ABI", nullptr, 0))
        dlssnr_abi_version = 99;
    return TRUE;
}
static_assert(std::is_same_v<decltype(&dlssnr_call_create), nr_forwarder::Create>);
static_assert(std::is_same_v<decltype(&dlssnr_call_evaluate), nr_forwarder::Evaluate>);
static_assert(std::is_same_v<decltype(&dlssnr_call_release), nr_forwarder::Release>);
