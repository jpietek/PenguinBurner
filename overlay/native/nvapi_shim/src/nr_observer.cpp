#include "nr_observer.h"
#include "../third_party/minhook/include/MinHook.h"

namespace { thread_local unsigned evaluation_depth = 0; }

bool nr_install_hooks(HMODULE module, NrHook* hooks, unsigned count) {
    unsigned created = 0;
    for (unsigned i = 0; i < count; ++i) {
        auto& hook = hooks[i];
        hook.address = reinterpret_cast<void*>(GetProcAddress(module, hook.name));
        if (!hook.address || MH_CreateHook(hook.address, hook.replacement, hook.original) != MH_OK) break;
        ++created;
    }
    if (created != count) {
        for (unsigned i = 0; i < created; ++i) MH_RemoveHook(hooks[i].address);
        return false;
    }
    for (unsigned i = 0; i < count; ++i) MH_QueueEnableHook(hooks[i].address);
    // If a batch partially enables before failing, retain the module and
    // trampolines. Every callback still has a valid original to forward to.
    return MH_ApplyQueued() == MH_OK;
}

void NrFeatures::created(ID3D12GraphicsCommandList* commands, const void* handle) {
    if (!handle) return;
    {
        std::lock_guard lock(mutex_);
        if (handles_.size() >= 1024 || !handles_.insert(handle).second) return;
    }
    // Preparing a pool calls into the runtime; never hold the tracker lock.
    nr_gpu_prepare(commands);
}

void NrFeatures::released(const void* handle) {
    std::lock_guard lock(mutex_);
    handles_.erase(handle);
}

bool NrFeatures::contains(const void* handle) {
    std::lock_guard lock(mutex_);
    return handles_.count(handle) != 0;
}

NrEvaluation::NrEvaluation(ID3D12GraphicsCommandList* commands, bool enabled) : active_(enabled) {
    if (active_ && evaluation_depth++ == 0) ticket_ = nr_gpu_begin(commands);
}

void NrEvaluation::finish(bool success) {
    if (!active_) return;
    nr_gpu_end(ticket_, success);
    --evaluation_depth;
    active_ = false;
}

NrEvaluation::~NrEvaluation() { finish(false); }
