#include <windows.h>
#include <d3d12.h>
#include <cstdio>
#include <cstdlib>
struct Params { ID3D12Resource* dst; ID3D12Resource* src; };
using Create=int(__cdecl*)(ID3D12GraphicsCommandList*,int,void*,void**);
using Eval=int(__cdecl*)(ID3D12GraphicsCommandList*,const void*,const void*,void*);
using Query=void*(__cdecl*)(unsigned);
#define CHECK(x) if(FAILED(x)) {std::printf("failed line %d\n",__LINE__);return 2;}
int main(int argc, char** argv) {
 const int feature_id = argc > 1 ? std::atoi(argv[1]) : 18;
 const unsigned frames = feature_id == 18 ? 100 : 4;
 const bool failed_eval = GetEnvironmentVariableA("PB_TEST_NGX_FAIL", nullptr, 0) != 0;
 HMODULE shim=LoadLibraryA("nvapi64.dll"); if(!shim)return 3;
 auto query=reinterpret_cast<Query>(GetProcAddress(shim,"nvapi_QueryInterface"));query(1);
 HMODULE ngx=LoadLibraryA("_nvngx.dll");if(!ngx)return 4;
 Sleep(750);
 ID3D12Device* device=nullptr;
 if(FAILED(D3D12CreateDevice(nullptr,D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device))))return 77;
 ID3D12CommandQueue* queue=nullptr;D3D12_COMMAND_QUEUE_DESC q{};q.Type=D3D12_COMMAND_LIST_TYPE_DIRECT;
 CHECK(device->CreateCommandQueue(&q,IID_PPV_ARGS(&queue)));
 ID3D12CommandAllocator* alloc=nullptr;CHECK(device->CreateCommandAllocator(q.Type,IID_PPV_ARGS(&alloc)));
 ID3D12GraphicsCommandList* list=nullptr;CHECK(device->CreateCommandList(0,q.Type,alloc,nullptr,IID_PPV_ARGS(&list)));
 ID3D12Fence* fence=nullptr;CHECK(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence)));
 D3D12_HEAP_PROPERTIES hp{};hp.Type=D3D12_HEAP_TYPE_UPLOAD;hp.CreationNodeMask=hp.VisibleNodeMask=1;
 D3D12_RESOURCE_DESC r{};r.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;r.Width=1024*1024;r.Height=r.DepthOrArraySize=r.MipLevels=1;r.SampleDesc.Count=1;r.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
 Params params{};
 CHECK(device->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&r,D3D12_RESOURCE_STATE_GENERIC_READ,nullptr,IID_PPV_ARGS(&params.src)));
 hp.Type=D3D12_HEAP_TYPE_DEFAULT;
 CHECK(device->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&r,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(&params.dst)));
 auto create=reinterpret_cast<Create>(GetProcAddress(ngx,"NVSDK_NGX_D3D12_CreateFeature"));
 auto eval=reinterpret_cast<Eval>(GetProcAddress(ngx,"NVSDK_NGX_D3D12_EvaluateFeature"));
 void* feature=nullptr;create(list,feature_id,nullptr,&feature);
 for(unsigned frame=1;frame<=frames;++frame) {
  if(frame>1){CHECK(alloc->Reset());CHECK(list->Reset(alloc,nullptr));}
  const int result = eval(list,feature,&params,nullptr);
  if ((!failed_eval && result != 1) || (failed_eval && result == 1)) return 5;
  CHECK(list->Close());ID3D12CommandList* submit[]={list};queue->ExecuteCommandLists(1,submit);
  CHECK(queue->Signal(fence,frame));while(fence->GetCompletedValue()<frame)Sleep(1);
  Sleep(12);
 }
 Sleep(100);
 std::printf("%u GPU evaluations completed; CPU delay is 40 ms per evaluate\n", frames);
 return 0;
}
