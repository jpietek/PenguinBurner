#pragma once
#include <windows.h>
#include <d3d12.h>

// Versioned NR forwarder ABI v2, shared by forks using nvngx.dll_dlssnr.dll.
// Only these call signatures are producer-specific; the collector is shared.
// ABI reference: gabrielmaialva33/retoque@82a4022b,
// retoque/dlssnr/forwarder/dlssnr_forwarder.cpp (dlssnr_abi_version = 2).
namespace nr_forwarder {
using Create = void* (__cdecl*)(const wchar_t*, const wchar_t*, ID3D12Device*,
    ID3D12GraphicsCommandList*, void*, unsigned, unsigned, int, float, int,
    float, float, float, int, int);
using Evaluate = int (__cdecl*)(ID3D12GraphicsCommandList*, void*, void*,
    ID3D12Resource*, ID3D12Resource*, ID3D12Resource*, ID3D12Resource*,
    unsigned, unsigned, unsigned, unsigned, unsigned, unsigned,
    unsigned, unsigned, unsigned, unsigned, int, int, float, int,
    float, float, float, int, float, float);
using Release = void (__cdecl*)(void*);
bool install(HMODULE module);
}
