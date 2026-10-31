#include <windows.h>
#include <d3d12.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include "../../overlay/native/nvapi_shim/src/nr_forwarder.h"
struct Params { ID3D12Resource* dst; ID3D12Resource* src; };
using Create=int(__cdecl*)(ID3D12GraphicsCommandList*,int,void*,void**);
using Eval=int(__cdecl*)(ID3D12GraphicsCommandList*,const void*,const void*,void*);
using Query=void*(__cdecl*)(unsigned);
using CreateQueue=HRESULT(STDMETHODCALLTYPE*)(ID3D12Device*,const D3D12_COMMAND_QUEUE_DESC*,REFIID,void**);
CreateQueue original_create_queue=nullptr;
unsigned queue_creations=0;
HRESULT STDMETHODCALLTYPE count_queue(ID3D12Device* device,const D3D12_COMMAND_QUEUE_DESC* desc,REFIID iid,void** out) {
 ++queue_creations;
 return original_create_queue(device,desc,iid,out);
}
using EndQuery=void(STDMETHODCALLTYPE*)(ID3D12GraphicsCommandList*,ID3D12QueryHeap*,D3D12_QUERY_TYPE,UINT);
EndQuery original_end_query=nullptr;
ID3D12GraphicsCommandList* callback_list=nullptr;
ID3D12CommandAllocator* callback_allocator=nullptr;
bool callback_failed=false;
void STDMETHODCALLTYPE reentrant_end_query(ID3D12GraphicsCommandList* list,ID3D12QueryHeap* heap,D3D12_QUERY_TYPE type,UINT index) {
 // Model a proxy/addon callback which resets its own command list. Both lists
 // share the runtime vtable, so this reenters the collector's Reset hook.
 if(FAILED(callback_list->Reset(callback_allocator,nullptr)) || FAILED(callback_list->Close())) callback_failed=true;
 original_end_query(list,heap,type,index);
}
#define CHECK(x) if(FAILED(x)) {std::printf("failed line %d\n",__LINE__);return 2;}
int main(int argc, char** argv) {
 const int feature_id = argc > 1 ? std::atoi(argv[1]) : 18;
 const unsigned frames = feature_id == 18 ? 100 : 4;
 const bool failed_eval = GetEnvironmentVariableA("PB_TEST_NGX_FAIL", nullptr, 0) != 0;
 const bool late = GetEnvironmentVariableA("PB_TEST_NR_LATE", nullptr, 0) != 0;
 const bool nested = GetEnvironmentVariableA("PB_TEST_NR_NESTED", nullptr, 0) != 0;
 const bool refused = GetEnvironmentVariableA("PB_TEST_NR_REFUSED", nullptr, 0) != 0;
 const bool forwarder = GetEnvironmentVariableA("PB_TEST_NR_FORWARDER", nullptr, 0) != 0;
 const bool unsupported = GetEnvironmentVariableA("PB_TEST_NR_UNKNOWN_ABI", nullptr, 0) != 0;
 // A real game can have its device and queues before NGX or our collector.
 // Never delay creation to give the collector an artificial head start.
 ID3D12Device* device=nullptr;
 if(FAILED(D3D12CreateDevice(nullptr,D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device))))return 77;
 auto table=*reinterpret_cast<void***>(device);
 DWORD old=0;CHECK(VirtualProtect(table+8,sizeof(void*),PAGE_READWRITE,&old) ? S_OK : E_FAIL);
 original_create_queue=reinterpret_cast<CreateQueue>(table[8]);
 InterlockedExchangePointer(table+8,reinterpret_cast<void*>(&count_queue));
 DWORD ignored=0;VirtualProtect(table+8,sizeof(void*),old,&ignored);
 ID3D12CommandQueue* queue=nullptr;D3D12_COMMAND_QUEUE_DESC q{};q.Type=D3D12_COMMAND_LIST_TYPE_DIRECT;
 CHECK(device->CreateCommandQueue(&q,IID_PPV_ARGS(&queue)));
 ID3D12CommandAllocator* alloc=nullptr;CHECK(device->CreateCommandAllocator(q.Type,IID_PPV_ARGS(&alloc)));
 ID3D12GraphicsCommandList* list=nullptr;CHECK(device->CreateCommandList(0,q.Type,alloc,nullptr,IID_PPV_ARGS(&list)));
 CHECK(device->CreateCommandAllocator(q.Type,IID_PPV_ARGS(&callback_allocator)));
 CHECK(device->CreateCommandList(0,q.Type,callback_allocator,nullptr,IID_PPV_ARGS(&callback_list)));
 CHECK(callback_list->Close());
 auto list_table=*reinterpret_cast<void***>(list);
 CHECK(VirtualProtect(list_table+53,sizeof(void*),PAGE_READWRITE,&old) ? S_OK : E_FAIL);
 original_end_query=reinterpret_cast<EndQuery>(list_table[53]);
 InterlockedExchangePointer(list_table+53,reinterpret_cast<void*>(&reentrant_end_query));
 VirtualProtect(list_table+53,sizeof(void*),old,&ignored);
 ID3D12Fence* fence=nullptr;CHECK(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence)));
 D3D12_HEAP_PROPERTIES hp{};hp.Type=D3D12_HEAP_TYPE_UPLOAD;hp.CreationNodeMask=hp.VisibleNodeMask=1;
 D3D12_RESOURCE_DESC r{};r.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;r.Width=1024*1024;r.Height=r.DepthOrArraySize=r.MipLevels=1;r.SampleDesc.Count=1;r.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
 Params params{};
 CHECK(device->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&r,D3D12_RESOURCE_STATE_GENERIC_READ,nullptr,IID_PPV_ARGS(&params.src)));
 hp.Type=D3D12_HEAP_TYPE_DEFAULT;
 CHECK(device->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&r,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(&params.dst)));
 HMODULE early=nullptr; void* feature=nullptr;
 if(late) {
  early=LoadLibraryA("nvngx.dll_dlssnr.dll"); if(!early)return 4;
  auto create=reinterpret_cast<nr_forwarder::Create>(GetProcAddress(early,"dlssnr_call_create"));
  feature=create(L"model",L"data",device,list,&params,640,480,2,.5f,1,.25f,.75f,1.f,1,0);
 }
 if(nested && !LoadLibraryA("_nvngx.dll"))return 4;
 HMODULE shim=LoadLibraryA("nvapi64.dll"); if(!shim)return 3;
 auto query=reinterpret_cast<Query>(GetProcAddress(shim,"nvapi_QueryInterface"));query(1);
 HMODULE ngx=LoadLibraryA(forwarder ? "nvngx.dll_dlssnr.dll" : "_nvngx.dll");if(!ngx)return 4;
 // Wait for the explicit hook-ready diagnostic, after graphics objects exist.
 // This tests late attachment, independently of the worker's polling period.
 if (std::getenv("PENGUIN_BURNER_NR_TIMING") && std::strcmp(std::getenv("PENGUIN_BURNER_NR_TIMING"),"1")==0) {
  bool ready=false;
  for(unsigned i=0;i<500 && !ready;++i) {
   FILE* f=std::fopen(std::getenv("PENGUIN_BURNER_SHIM_OUTPUT"),"rb");
   if(f){char buf[4096]{};auto n=std::fread(buf,1,sizeof(buf)-1,f);std::fclose(f);std::string text(buf,n);ready=text.find(unsupported ? "observer unavailable" : (forwarder ? "forwarder ABI v2 observer ready" : "feature-18 observer ready"))!=std::string::npos;
    if(nested)ready=ready && text.find("feature-18 observer ready")!=std::string::npos;}
   if(!ready)Sleep(10);
  }
  if(!ready){std::puts("NGX observer did not become ready");return 8;}
 }
 auto create=reinterpret_cast<Create>(GetProcAddress(ngx,"NVSDK_NGX_D3D12_CreateFeature"));
 auto eval=reinterpret_cast<Eval>(GetProcAddress(ngx,"NVSDK_NGX_D3D12_EvaluateFeature"));
 auto forward_create=reinterpret_cast<nr_forwarder::Create>(GetProcAddress(ngx,"dlssnr_call_create"));
 auto forward_eval=reinterpret_cast<nr_forwarder::Evaluate>(GetProcAddress(ngx,"dlssnr_call_evaluate"));
 if (forwarder && !late) feature=forward_create(L"model",L"data",device,list,&params,640,480,2,.5f,1,.25f,.75f,1.f,1,0);
 else if (!forwarder) create(list,feature_id,nullptr,&feature);
 if(!feature)return 9;
 unsigned prepared_queues=queue_creations;
 for(unsigned frame=1;frame<=frames;++frame) {
  if(frame>1){CHECK(alloc->Reset());CHECK(list->Reset(alloc,nullptr));}
  const int result = forwarder ? forward_eval(list,feature,&params,nullptr,nullptr,nullptr,nullptr,
      640,480,320,240,160,120,2,3,4,5,1,0,.5f,1,.25f,.75f,1.f,1,-1.f,2.f)
      : eval(list,feature,&params,nullptr);
  if (refused ? result != 0 : ((!failed_eval && result != 1) || (failed_eval && result == 1))) return 5;
  if(late && frame==1)prepared_queues=queue_creations;
  CHECK(list->Close());ID3D12CommandList* submit[]={list};queue->ExecuteCommandLists(1,submit);
  CHECK(queue->Signal(fence,frame));while(fence->GetCompletedValue()<frame)Sleep(1);
  Sleep(12);
 }
 Sleep(100);
 if(callback_failed) return 7;
 if(queue_creations!=prepared_queues) {
  std::printf("collector created %u queues during evaluation\n",queue_creations-prepared_queues);
  return 6;
 }
 std::printf("%u GPU evaluations completed; CPU delay is 40 ms per evaluate\n", frames);
 return 0;
}
