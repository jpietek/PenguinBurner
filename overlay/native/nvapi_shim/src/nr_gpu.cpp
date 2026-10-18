// NGX D3D12 evaluation timing. Queries belong to a command-list recording until
// Reset, and to submitted GPU work until its fence completes. Never recycle a
// query just because a number of CPU frames elapsed; lists can be replayed.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "nr_gpu.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <vector>

namespace {
constexpr unsigned kSlots = 64;
constexpr unsigned kMaxDevices = 8;
using Execute = void (STDMETHODCALLTYPE*)(ID3D12CommandQueue*, UINT, ID3D12CommandList* const*);
using Reset = HRESULT (STDMETHODCALLTYPE*)(ID3D12GraphicsCommandList*, ID3D12CommandAllocator*, ID3D12PipelineState*);
struct QueueTap { void** table; Execute original; };
struct ListTap { void** table; Reset original; bool ready = false; };
struct Slot {
    ID3D12GraphicsCommandList* owner = nullptr;
    ID3D12Fence* fence = nullptr;
    uint64_t value = 0;
    uint64_t published = 0;
    uint64_t frequency = 0;
    uint64_t serial = 0;
    bool occupied = false;
    bool valid = false;
    bool submitting = false;
    bool submitted = false;
    bool quarantined = false;
};
struct Pool {
    ID3D12Device* device = nullptr;
    ID3D12QueryHeap* queries = nullptr;
    ID3D12Resource* readback = nullptr;
    uint64_t* mapped = nullptr;
    std::array<Slot, kSlots> slots{};
};
// Intentionally process-lived: GPU work and recorded lists may outlive NGX
// features. Capacity is bounded even if an application abandons a recording.
std::mutex g_mutex;
std::vector<std::unique_ptr<Pool>> g_pools;
std::vector<QueueTap> g_queues;
std::vector<ListTap> g_lists;
uint64_t g_serial = 0;

void STDMETHODCALLTYPE execute(ID3D12CommandQueue* queue, UINT count, ID3D12CommandList* const* lists) {
    Execute original = nullptr;
    struct Pending { Pool* pool; unsigned index; };
    std::array<Pending, kSlots * kMaxDevices> pending{};
    size_t used = 0;
    uint64_t frequency = 0;
    queue->GetTimestampFrequency(&frequency);
    {
        std::lock_guard lock(g_mutex);
        auto table = *reinterpret_cast<void***>(queue);
        for (const auto& tap : g_queues) if (tap.table == table) original = tap.original;
        for (const auto& pool : g_pools) {
            for (unsigned i = 0; i < kSlots; ++i) {
                auto& s = pool->slots[i];
                if (!s.occupied || !s.owner || s.quarantined) continue;
                for (UINT n = 0; n < count; ++n) {
                    if (lists[n] != s.owner) continue;
                    // Concurrent replay cannot share query storage safely.
                    // Keep the recording reserved, suppress its measurement.
                    if (s.submitting || (s.submitted && s.fence->GetCompletedValue() < s.value)) {
                        s.valid = false;
                        s.quarantined = true;
                        break;
                    }
                    s.submitting = true;
                    s.frequency = frequency;
                    pending[used++] = {pool.get(), i};
                    break;
                }
            }
        }
    }
    if (original) original(queue, count, lists);
    {
        std::lock_guard lock(g_mutex);
        for (size_t n = 0; n < used; ++n) {
            auto& s = pending[n].pool->slots[pending[n].index];
            ++s.value;
            s.serial = ++g_serial;
            s.submitted = true;
            if (FAILED(queue->Signal(s.fence, s.value))) {
                s.valid = false;
                s.quarantined = true;
            }
            s.submitting = false;
        }
    }
}

HRESULT STDMETHODCALLTYPE reset(ID3D12GraphicsCommandList* commands,
    ID3D12CommandAllocator* allocator, ID3D12PipelineState* initial) {
    Reset original = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto table = *reinterpret_cast<void***>(commands);
        for (const auto& tap : g_lists) if (tap.table == table) original = tap.original;
    }
    const HRESULT result = original ? original(commands, allocator, initial) : E_FAIL;
    if (SUCCEEDED(result)) {
        std::lock_guard lock(g_mutex);
        for (const auto& pool : g_pools) {
            for (auto& s : pool->slots) {
                if (s.owner == commands) {
                    s.owner = nullptr;
                    commands->Release();
                }
            }
        }
    }
    return result;
}

bool patch_slot(void** table, unsigned index, void* replacement) {
    DWORD old = 0;
    if (!VirtualProtect(table + index, sizeof(void*), PAGE_READWRITE, &old)) return false;
    InterlockedExchangePointer(table + index, replacement);
    DWORD ignored = 0;
    VirtualProtect(table + index, sizeof(void*), old, &ignored);
    return true;
}

bool tap_commands(ID3D12GraphicsCommandList* commands, ID3D12Device* device) {
    auto list_table = *reinterpret_cast<void***>(commands);
    bool found = false;
    for (const auto& tap : g_lists) found |= tap.table == list_table;
    if (!found) {
        if (g_lists.size() >= 32) return false;
        // IUnknown / ID3D12Object / ID3D12DeviceChild / ID3D12CommandList ABI.
        const auto original = reinterpret_cast<Reset>(list_table[10]);
        g_lists.push_back({list_table, original});
        if (!patch_slot(list_table, 10, reinterpret_cast<void*>(&reset))) {
            g_lists.pop_back();
            return false;
        }
    }
    D3D12_COMMAND_QUEUE_DESC desc{};
    desc.Type = commands->GetType();
    ID3D12CommandQueue* queue = nullptr;
    if (FAILED(device->CreateCommandQueue(&desc, IID_PPV_ARGS(&queue)))) return false;
    auto table = *reinterpret_cast<void***>(queue);
    found = false;
    for (const auto& tap : g_queues) found |= tap.table == table;
    bool ok = true;
    if (!found) {
        if (g_queues.size() >= 32) ok = false;
        else {
            auto original = reinterpret_cast<Execute>(table[10]);
            g_queues.push_back({table, original});
            if (!patch_slot(table, 10, reinterpret_cast<void*>(&execute))) {
                g_queues.pop_back();
                ok = false;
            }
        }
    }
    queue->Release();
    if (ok) for (auto& tap : g_lists) if (tap.table == list_table) tap.ready = true;
    return ok;
}

std::unique_ptr<Pool> make_pool(ID3D12Device* device) {
    auto pool = std::make_unique<Pool>();
    pool->device = device;
    device->AddRef();
    D3D12_QUERY_HEAP_DESC query{};
    query.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    query.Count = kSlots * 2;
    auto failed = [&pool]() -> std::unique_ptr<Pool> {
        for (auto& s : pool->slots) if (s.fence) s.fence->Release();
        if (pool->readback) {
            if (pool->mapped) pool->readback->Unmap(0, nullptr);
            pool->readback->Release();
        }
        if (pool->queries) pool->queries->Release();
        pool->device->Release();
        return nullptr;
    };
    if (FAILED(device->CreateQueryHeap(&query, IID_PPV_ARGS(&pool->queries)))) return failed();
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = kSlots * 2 * sizeof(uint64_t);
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&pool->readback)))) return failed();
    if (FAILED(pool->readback->Map(0, nullptr, reinterpret_cast<void**>(&pool->mapped)))) return failed();
    for (auto& s : pool->slots) {
        if (FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&s.fence)))) return failed();
    }
    return pool;
}
} // namespace

NrGpuTicket nr_gpu_begin(ID3D12GraphicsCommandList* commands) {
    if (!commands || (commands->GetType() != D3D12_COMMAND_LIST_TYPE_DIRECT &&
                      commands->GetType() != D3D12_COMMAND_LIST_TYPE_COMPUTE)) return {};
    ID3D12Device* device = nullptr;
    if (FAILED(commands->GetDevice(IID_PPV_ARGS(&device)))) return {};
    std::lock_guard lock(g_mutex);
    Pool* pool = nullptr;
    for (const auto& entry : g_pools) if (entry->device == device) pool = entry.get();
    bool tapped = false;
    const auto table = *reinterpret_cast<void***>(commands);
    for (const auto& tap : g_lists) tapped |= tap.table == table && tap.ready;
    if (!tapped) tapped = tap_commands(commands, device);
    if (!pool && g_pools.size() < kMaxDevices && tapped) {
        auto created = make_pool(device);
        if (created) {
            pool = created.get();
            g_pools.push_back(std::move(created));
        }
    }
    device->Release();
    if (!pool || !tapped) return {};
    for (unsigned i = 0; i < kSlots; ++i) {
        auto& s = pool->slots[i];
        if (s.occupied) continue;
        s.occupied = true;
        s.owner = commands;
        commands->AddRef();
        s.valid = false;
        s.submitted = false;
        commands->EndQuery(pool->queries, D3D12_QUERY_TYPE_TIMESTAMP, i * 2);
        return {pool, i};
    }
    return {};
}

void nr_gpu_end(NrGpuTicket ticket, bool successful) {
    if (!ticket.pool) return;
    std::lock_guard lock(g_mutex);
    auto* pool = static_cast<Pool*>(ticket.pool);
    auto& s = pool->slots[ticket.slot];
    s.owner->EndQuery(pool->queries, D3D12_QUERY_TYPE_TIMESTAMP, ticket.slot * 2 + 1);
    s.owner->ResolveQueryData(pool->queries, D3D12_QUERY_TYPE_TIMESTAMP,
        ticket.slot * 2, 2, pool->readback, ticket.slot * 2 * sizeof(uint64_t));
    s.valid = successful;
}

void nr_gpu_poll(NrEmit emit) {
    std::lock_guard lock(g_mutex);
    for (const auto& pool : g_pools) {
        for (unsigned i = 0; i < kSlots; ++i) {
            auto& s = pool->slots[i];
            if (!s.occupied || s.submitting || s.quarantined) continue;
            const uint64_t completed = s.fence->GetCompletedValue();
            if (s.submitted && completed < s.value) continue;
            if (s.submitted && s.valid && completed != UINT64_MAX && s.published != s.value && s.frequency) {
                const uint64_t begin = pool->mapped[i * 2], end = pool->mapped[i * 2 + 1];
                const double us = end >= begin ? double(end - begin) * 1e6 / double(s.frequency) : 0.0;
                if (us > 0.0 && us < 1000000.0) {
                    char text[192];
                    const int len = std::snprintf(text, sizeof(text),
                        "pb-nr-gpu: pid=%lu device=%p sample=%llu gpuUs=%.3f api=d3d12 scope=model\n",
                        GetCurrentProcessId(), pool->device,
                        static_cast<unsigned long long>(s.serial), us);
                    if (len > 0 && len < int(sizeof(text))) emit(text, len);
                }
                s.published = s.value;
            }
            if (!s.owner) {
                s.occupied = false;
                s.valid = false;
            }
        }
    }
}
