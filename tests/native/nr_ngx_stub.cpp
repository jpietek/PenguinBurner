#include <windows.h>
#include <d3d12.h>
struct Params { ID3D12Resource* dst; ID3D12Resource* src; };
extern "C" __declspec(dllexport) int NVSDK_NGX_D3D12_CreateFeature(ID3D12GraphicsCommandList*, int feature, void*, void** out) { *out=reinterpret_cast<void*>(static_cast<uintptr_t>(feature)); return 1; }
extern "C" __declspec(dllexport) int NVSDK_NGX_D3D12_ReleaseFeature(void*) { return 1; }
extern "C" __declspec(dllexport) int NVSDK_NGX_D3D12_EvaluateFeature(ID3D12GraphicsCommandList* list,const void*,const void* param,void*) {
 Sleep(40);
 auto* p=static_cast<const Params*>(param);
 for(int i=0;i<8;++i) list->CopyBufferRegion(p->dst,0,p->src,0,1024*1024);
 return GetEnvironmentVariableA("PB_TEST_NGX_FAIL", nullptr, 0) ? static_cast<int>(0xbad00001u) : 1;
}
