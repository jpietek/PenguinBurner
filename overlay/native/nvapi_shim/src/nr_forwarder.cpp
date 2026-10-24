#include "nr_forwarder.h"
#include "nr_observer.h"

namespace nr_forwarder {
namespace {
Create original_create = nullptr;
Evaluate original_evaluate = nullptr;
Release original_release = nullptr;
NrFeatures features;

void* __cdecl create(const wchar_t* model, const wchar_t* data, ID3D12Device* device,
    ID3D12GraphicsCommandList* commands, void* params, unsigned width, unsigned height,
    int preset, float intensity, int style, float structure, float tone, float skin,
    int auto_mask, int ui_correction) {
    void* handle = original_create(model, data, device, commands, params, width, height,
        preset, intensity, style, structure, tone, skin, auto_mask, ui_correction);
    features.created(commands, handle);
    return handle;
}

int __cdecl evaluate(ID3D12GraphicsCommandList* commands, void* handle, void* params,
    ID3D12Resource* color, ID3D12Resource* depth, ID3D12Resource* motion, ID3D12Resource* output,
    unsigned width, unsigned height, unsigned guide_width, unsigned guide_height,
    unsigned motion_width, unsigned motion_height, unsigned depth_x, unsigned depth_y,
    unsigned motion_x, unsigned motion_y, int inverted, int reset, float intensity, int style,
    float structure, float tone, float skin, int auto_mask, float mv_x, float mv_y) {
    // LoadLibrary and the first create can finish before the observer thread
    // sees the module. Prepare that existing handle once, without requiring a
    // game restart or polling/creating queues on every evaluation.
    if (handle && params && !features.contains(handle)) features.created(commands, handle);
    NrEvaluation timing(commands, handle && params && features.contains(handle));
    const int result = original_evaluate(commands, handle, params, color, depth, motion, output,
        width, height, guide_width, guide_height, motion_width, motion_height,
        depth_x, depth_y, motion_x, motion_y, inverted, reset, intensity, style,
        structure, tone, skin, auto_mask, mv_x, mv_y);
    timing.finish(result == 1);
    return result;
}

void __cdecl release(void* handle) {
    original_release(handle);
    features.released(handle);
}
}  // namespace

bool install(HMODULE module) {
    // This is an exported integer, not a function. Unknown ABI revisions are
    // left untouched rather than guessing the stack layout of 27 arguments.
    const auto* version = reinterpret_cast<const int*>(GetProcAddress(module, "dlssnr_abi_version"));
    if (!version || IsBadReadPtr(version, sizeof(*version)) || *version != 2) return false;
    NrHook hooks[] = {
        {"dlssnr_call_create", reinterpret_cast<void*>(&create), reinterpret_cast<void**>(&original_create)},
        {"dlssnr_call_evaluate", reinterpret_cast<void*>(&evaluate), reinterpret_cast<void**>(&original_evaluate)},
        {"dlssnr_call_release", reinterpret_cast<void*>(&release), reinterpret_cast<void**>(&original_release)},
    };
    return nr_install_hooks(module, hooks, 3);
}
}  // namespace nr_forwarder
