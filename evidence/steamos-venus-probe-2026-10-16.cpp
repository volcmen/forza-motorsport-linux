#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstdlib>

static void check(HRESULT hr, const char *step) {
    if (FAILED(hr)) { std::printf("FAIL %s HRESULT=0x%08lx\n", step, (unsigned long)hr); std::fflush(stdout); std::exit(2); }
    std::printf("OK %s\n",step); std::fflush(stdout);
}
static LRESULT CALLBACK wndproc(HWND w, UINT m, WPARAM a, LPARAM b) {
    return DefWindowProcW(w, m, a, b);
}
int main(int argc, char **argv) {
    if (argc != 2 || !std::freopen(argv[1], "w", stdout)) return 6;
    WNDCLASSW wc{}; wc.lpfnWndProc=wndproc; wc.hInstance=GetModuleHandleW(nullptr); wc.lpszClassName=L"ForzaVmGraphicsProbe";
    if (!RegisterClassW(&wc)) return 3;
    HWND window=CreateWindowW(wc.lpszClassName,L"VM D3D12 readback and presentation probe",WS_OVERLAPPEDWINDOW|WS_VISIBLE,100,100,660,440,nullptr,nullptr,wc.hInstance,nullptr);
    if (!window) return 3;
    IDXGIFactory4 *factory=nullptr; ID3D12Device *device=nullptr;
    check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)),"factory");
    check(D3D12CreateDevice(nullptr,D3D_FEATURE_LEVEL_12_0,IID_PPV_ARGS(&device)),"D3D12 device FL12_0");
    ID3D12CommandQueue *queue=nullptr; D3D12_COMMAND_QUEUE_DESC qd{};
    check(device->CreateCommandQueue(&qd,IID_PPV_ARGS(&queue)),"queue");
    DXGI_SWAP_CHAIN_DESC1 sd{}; sd.Width=640; sd.Height=400; sd.Format=DXGI_FORMAT_R8G8B8A8_UNORM; sd.SampleDesc.Count=1; sd.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT; sd.BufferCount=2; sd.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;
    IDXGISwapChain1 *swap=nullptr;
    check(factory->CreateSwapChainForHwnd(queue,window,&sd,nullptr,nullptr,&swap),"swapchain");
    ID3D12Resource *back=nullptr;
    check(swap->GetBuffer(0,IID_PPV_ARGS(&back)),"backbuffer");
    D3D12_DESCRIPTOR_HEAP_DESC hd{}; hd.Type=D3D12_DESCRIPTOR_HEAP_TYPE_RTV; hd.NumDescriptors=1;
    ID3D12DescriptorHeap *heap=nullptr;
    check(device->CreateDescriptorHeap(&hd,IID_PPV_ARGS(&heap)),"RTV heap");
    auto rtv=heap->GetCPUDescriptorHandleForHeapStart(); device->CreateRenderTargetView(back,nullptr,rtv);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{}; UINT64 bytes=0; auto desc=back->GetDesc();
    device->GetCopyableFootprints(&desc,0,1,0,&footprint,nullptr,nullptr,&bytes);
    D3D12_HEAP_PROPERTIES hp{}; hp.Type=D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC rd{}; rd.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER; rd.Width=bytes; rd.Height=1; rd.DepthOrArraySize=1; rd.MipLevels=1; rd.SampleDesc.Count=1; rd.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *readback=nullptr;
    check(device->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&rd,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(&readback)),"readback resource");
    ID3D12CommandAllocator *allocator=nullptr; ID3D12GraphicsCommandList *list=nullptr;
    check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,IID_PPV_ARGS(&allocator)),"allocator");
    check(device->CreateCommandList(0,D3D12_COMMAND_LIST_TYPE_DIRECT,allocator,nullptr,IID_PPV_ARGS(&list)),"command list");
    D3D12_RESOURCE_BARRIER barrier{}; barrier.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; barrier.Transition.pResource=back; barrier.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; barrier.Transition.StateBefore=D3D12_RESOURCE_STATE_PRESENT; barrier.Transition.StateAfter=D3D12_RESOURCE_STATE_RENDER_TARGET;
    list->ResourceBarrier(1,&barrier);
    const FLOAT color[4]={0.25f,0.5f,0.75f,1.0f}; list->ClearRenderTargetView(rtv,color,0,nullptr);
    barrier.Transition.StateBefore=D3D12_RESOURCE_STATE_RENDER_TARGET; barrier.Transition.StateAfter=D3D12_RESOURCE_STATE_COPY_SOURCE; list->ResourceBarrier(1,&barrier);
    D3D12_TEXTURE_COPY_LOCATION src{},dst{}; src.pResource=back; src.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.pResource=readback; dst.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dst.PlacedFootprint=footprint;
    list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);
    barrier.Transition.StateBefore=D3D12_RESOURCE_STATE_COPY_SOURCE; barrier.Transition.StateAfter=D3D12_RESOURCE_STATE_PRESENT; list->ResourceBarrier(1,&barrier);
    check(list->Close(),"close list"); ID3D12CommandList *lists[]={list}; queue->ExecuteCommandLists(1,lists);
    ID3D12Fence *fence=nullptr; check(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence)),"fence");
    HANDLE event=CreateEventW(nullptr,FALSE,FALSE,nullptr); if (!event) return 3;
    check(queue->Signal(fence,1),"signal"); check(fence->SetEventOnCompletion(1,event),"fence event");
    if (WaitForSingleObject(event,30000)!=WAIT_OBJECT_0) { std::puts("FAIL fence timeout"); return 4; }
    D3D12_RANGE range{0,(SIZE_T)bytes}; void *mapped=nullptr; check(readback->Map(0,&range,&mapped),"map readback");
    auto pixel=(unsigned char *)mapped+footprint.Offset+200*footprint.Footprint.RowPitch+320*4;
    bool correct=abs((int)pixel[0]-64)<=1 && abs((int)pixel[1]-128)<=1 && abs((int)pixel[2]-191)<=1 && pixel[3]==255;
    std::printf("Readback RGBA=%u,%u,%u,%u expected=64,128,191,255: %s\n",pixel[0],pixel[1],pixel[2],pixel[3],correct?"PASS":"FAIL");
    D3D12_RANGE wrote{0,0}; readback->Unmap(0,&wrote);
    check(swap->Present(1,0),"present"); std::puts("Fence completed; Present returned success. Expected visible window: solid muted blue."); std::fflush(stdout);
    auto until=GetTickCount64()+8000; MSG msg{};
    while (GetTickCount64()<until) { while (PeekMessageW(&msg,nullptr,0,0,PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); } Sleep(20); }
    std::puts("BEGIN CloseHandle(event)"); std::fflush(stdout); CloseHandle(event); std::puts("DONE CloseHandle(event)"); std::fflush(stdout);
    std::puts("BEGIN fence->Release()"); std::fflush(stdout); fence->Release(); std::puts("DONE fence->Release()"); std::fflush(stdout);
    std::puts("BEGIN list->Release()"); std::fflush(stdout); list->Release(); std::puts("DONE list->Release()"); std::fflush(stdout);
    std::puts("BEGIN allocator->Release()"); std::fflush(stdout); allocator->Release(); std::puts("DONE allocator->Release()"); std::fflush(stdout);
    std::puts("BEGIN readback->Release()"); std::fflush(stdout); readback->Release(); std::puts("DONE readback->Release()"); std::fflush(stdout);
    std::puts("BEGIN heap->Release()"); std::fflush(stdout); heap->Release(); std::puts("DONE heap->Release()"); std::fflush(stdout);
    std::puts("BEGIN back->Release()"); std::fflush(stdout); back->Release(); std::puts("DONE back->Release()"); std::fflush(stdout);
    std::puts("BEGIN swap->Release()"); std::fflush(stdout); swap->Release(); std::puts("DONE swap->Release()"); std::fflush(stdout);
    std::puts("BEGIN queue->Release()"); std::fflush(stdout); queue->Release(); std::puts("DONE queue->Release()"); std::fflush(stdout);
    std::puts("BEGIN device->Release()"); std::fflush(stdout); device->Release(); std::puts("DONE device->Release()"); std::fflush(stdout);
    std::puts("BEGIN factory->Release()"); std::fflush(stdout); factory->Release(); std::puts("DONE factory->Release()"); std::fflush(stdout);
    std::puts("BEGIN DestroyWindow(window)"); std::fflush(stdout); DestroyWindow(window); std::puts("DONE DestroyWindow(window)"); std::fflush(stdout);
    std::puts("DONE main returning"); std::fflush(stdout);
    return correct?0:5;
}
