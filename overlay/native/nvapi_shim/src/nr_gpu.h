#pragma once
#include <d3d12.h>
#include "nr_telemetry.h"

struct NrGpuTicket {
    void* pool = nullptr;
    unsigned slot = 0;
};
NrGpuTicket nr_gpu_begin(ID3D12GraphicsCommandList* commands);
void nr_gpu_end(NrGpuTicket ticket, bool successful);
void nr_gpu_poll(NrEmit emit);

bool nr_gpu_prepare(ID3D12GraphicsCommandList* commands);
