#pragma once
#include <windows.h>
#include <mutex>
#include <unordered_set>
#include "nr_gpu.h"

struct NrHook {
    const char* name;
    void* replacement;
    void** original;
    void* address = nullptr;
};
bool nr_install_hooks(HMODULE module, NrHook* hooks, unsigned count);

// Each ABI adapter owns its handles; GPU pools, timestamps and telemetry are
// shared. A late-attached adapter may prepare a handle at its first evaluation.
class NrFeatures {
public:
    void created(ID3D12GraphicsCommandList* commands, const void* handle);
    void released(const void* handle);
    bool contains(const void* handle);
private:
    std::mutex mutex_;
    std::unordered_set<const void*> handles_;
};

// Nested producer paths must not count the same model evaluation twice.
class NrEvaluation {
public:
    NrEvaluation(ID3D12GraphicsCommandList* commands, bool enabled);
    ~NrEvaluation();
    void finish(bool success);
    NrEvaluation(const NrEvaluation&) = delete;
    NrEvaluation& operator=(const NrEvaluation&) = delete;
private:
    NrGpuTicket ticket_{};
    bool active_ = false;
};
