// Copyright (C) 2026 Alexander Eichhorn
// SPDX-License-Identifier: GPL-3.0-only
// Typed SDK bindings; no private entry points or manually indexed COM vtables.
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <windows.h>
#include <xpsobjectmodel_1.h>
#include <xpsrassvc.h>
#include <wincodec.h>
#include <wrl/client.h>
#include <vector>
#include <string>
#include <cmath>
#include <limits>
#include <atomic>

using Microsoft::WRL::ComPtr;
namespace {
struct Failure { HRESULT hr; };
void checked(HRESULT hr) { if (FAILED(hr)) throw Failure{hr}; }
struct Unlocked {
    PyThreadState* state = PyEval_SaveThread();
    ~Unlocked() { PyEval_RestoreThread(state); }
};
class Notification final : public IXpsRasterizerNotificationCallback {
    std::atomic<ULONG> refs{1};
    PyObject* callable;
    PyObject *type=nullptr, *value=nullptr, *traceback=nullptr;
public:
    explicit Notification(PyObject* callback): callable(callback) { Py_INCREF(callable); }
    ~Notification() {
        auto state=PyGILState_Ensure();
        Py_DECREF(callable);Py_XDECREF(type);Py_XDECREF(value);Py_XDECREF(traceback);
        PyGILState_Release(state);
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out=nullptr;
        if (iid!=__uuidof(IUnknown) && iid!=__uuidof(IXpsRasterizerNotificationCallback)) return E_NOINTERFACE;
        *out=static_cast<IXpsRasterizerNotificationCallback*>(this);AddRef();return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG n=--refs;if (!n) delete this;return n;
    }
    HRESULT STDMETHODCALLTYPE Continue() override {
        auto state=PyGILState_Ensure();
        if (!type) {
            PyObject* result=PyObject_CallNoArgs(callable);
            if (!result) PyErr_Fetch(&type,&value,&traceback);
            else Py_DECREF(result);
        }
        bool failed=type!=nullptr;
        PyGILState_Release(state);
        return failed?HRESULT_FROM_WIN32(ERROR_CANCELLED):S_OK;
    }
    bool restore_error() {
        if (!type) return false;
        PyErr_Restore(type,value,traceback);type=value=traceback=nullptr;return true;
    }
};
// Registered system class, tested at runtime. Its standalone activation is
// not promised by the XPSDrv documentation; failures are explicit, never 8-bit fallback.
const CLSID RasterFactory = {0x503e79bf,0x1d09,0x4764,{0x9d,0x72,0x1e,0xb0,0xc6,0x59,0x67,0xc6}};
struct Page {
    ComPtr<IXpsOMPageReference> reference;
    ComPtr<IXpsOMDocument> document;
};
struct Document {
    DWORD thread = GetCurrentThreadId();
    bool apartment = false;
    ComPtr<IXpsOMPackage1> package;
    ComPtr<IXpsOMDocumentSequence> sequence;
    ComPtr<IXpsRasterizationFactory2> factory;
    ComPtr<IXpsRasterizer> rasterizer;
    std::vector<Page> pages;
    ~Document() {
        rasterizer.Reset(); pages.clear(); factory.Reset(); sequence.Reset(); package.Reset();
        if (apartment) CoUninitialize();
    }
    void initialize(const wchar_t* filename) {
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (hr != RPC_E_CHANGED_MODE) { checked(hr); apartment = true; }
        ComPtr<IXpsOMObjectFactory1> objects;
        checked(CoCreateInstance(__uuidof(XpsOMObjectFactory), nullptr,
            CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&objects)));
        checked(objects->CreatePackageFromFile1(filename, TRUE, &package));
        checked(CoCreateInstance(RasterFactory, nullptr, CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(&factory)));
        checked(package->GetDocumentSequence(&sequence));
        ComPtr<IXpsOMDocumentCollection> documents;
        checked(sequence->GetDocuments(&documents));
        UINT32 count = 0; checked(documents->GetCount(&count));
        for (UINT32 i=0; i<count; ++i) {
            ComPtr<IXpsOMDocument> doc; checked(documents->GetAt(i, &doc));
            ComPtr<IXpsOMPageReferenceCollection> refs;
            checked(doc->GetPageReferences(&refs));
            UINT32 n=0; checked(refs->GetCount(&n));
            for (UINT32 j=0; j<n; ++j) {
                Page page; page.document=doc; checked(refs->GetAt(j,&page.reference));
                pages.push_back(std::move(page));
            }
        }
        if (pages.empty()) throw Failure{E_INVALIDARG};
    }
    Page& at(Py_ssize_t i) {
        if (i<0 || static_cast<size_t>(i)>=pages.size()) throw Failure{E_INVALIDARG};
        return pages[static_cast<size_t>(i)];
    }
};
Document* document(PyObject* object) {
    auto d=static_cast<Document*>(PyCapsule_GetPointer(object,"pytone.xps.document"));
    if (d && d->thread!=GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError,"XPS session must stay on its creating thread");
        return nullptr;
    }
    return d;
}
void destroy(PyObject* object) {
    if (PyCapsule_IsValid(object,"pytone.xps.document"))
        delete static_cast<Document*>(PyCapsule_GetPointer(object,"pytone.xps.document"));
}
PyObject* error(HRESULT hr) {
    PyErr_Format(PyExc_OSError,"Windows high-precision XPS renderer failed (HRESULT 0x%08x)",
                 static_cast<unsigned int>(hr));
    return nullptr;
}
PyObject* ticket(IXpsOMPrintTicketResource* resource) {
    if (!resource) return PyBytes_FromStringAndSize("",0);
    ComPtr<IStream> stream; checked(resource->GetStream(&stream));
    STATSTG stat{}; checked(stream->Stat(&stat,STATFLAG_NONAME));
    if (stat.cbSize.QuadPart>16*1024*1024) throw Failure{E_INVALIDARG};
    checked(stream->Seek({},STREAM_SEEK_SET,nullptr));
    std::string data(static_cast<size_t>(stat.cbSize.QuadPart),'\0');
    ULONG read=0; checked(stream->Read(data.data(),static_cast<ULONG>(data.size()),&read));
    if (read!=data.size()) throw Failure{E_FAIL};
    return PyBytes_FromStringAndSize(data.data(),data.size());
}
PyObject* open_document(PyObject*,PyObject* filename) {
    wchar_t* path=PyUnicode_AsWideCharString(filename,nullptr);
    if (!path) return nullptr;
    Document* d=nullptr;
    try {
        d=new Document();
        { Unlocked unlock; d->initialize(path); }
        PyMem_Free(path);
        auto result=PyCapsule_New(d,"pytone.xps.document",destroy);
        if (!result) delete d;
        return result;
    } catch (Failure e) { delete d; PyMem_Free(path); return error(e.hr); }
      catch (std::bad_alloc&) { delete d; PyMem_Free(path); return PyErr_NoMemory(); }
}
PyObject* close_document(PyObject*,PyObject* object) {
    auto d=document(object); if (!d) return nullptr;
    delete d; PyCapsule_SetDestructor(object,nullptr);
    PyCapsule_SetName(object,"pytone.xps.closed"); Py_RETURN_NONE;
}
PyObject* count_pages(PyObject*,PyObject* object) {
    auto d=document(object); return d?PyLong_FromSize_t(d->pages.size()):nullptr;
}
PyObject* describe_page(PyObject*,PyObject* args) {
    PyObject* object; Py_ssize_t index;
    if (!PyArg_ParseTuple(args,"On",&object,&index)) return nullptr;
    auto d=document(object); if (!d) return nullptr;
    try {
        auto& item=d->at(index); ComPtr<IXpsOMPage> page;
        checked(item.reference->GetPage(&page));
        XPS_SIZE size{}; checked(page->GetPageDimensions(&size));
        ComPtr<IXpsOMPrintTicketResource> a,b,c;
        checked(d->sequence->GetPrintTicketResource(&a));
        checked(item.document->GetPrintTicketResource(&b));
        checked(item.reference->GetPrintTicketResource(&c));
        PyObject* ta=ticket(a.Get()); PyObject* tb=ticket(b.Get()); PyObject* tc=ticket(c.Get());
        if (!ta || !tb || !tc) { Py_XDECREF(ta);Py_XDECREF(tb);Py_XDECREF(tc);return nullptr; }
        return Py_BuildValue("(ffNNN)",size.width,size.height,ta,tb,tc);
    } catch (Failure e) { return error(e.hr); }
      catch (std::bad_alloc&) { return PyErr_NoMemory(); }
}
PyObject* begin_page(PyObject*,PyObject* args) {
    PyObject* object; Py_ssize_t index; float x,y;
    if (!PyArg_ParseTuple(args,"Onff",&object,&index,&x,&y)) return nullptr;
    auto d=document(object); if (!d) return nullptr;
    if (!std::isfinite(x)||!std::isfinite(y)||x<=0||y<=0) return error(E_INVALIDARG);
    try {
        ComPtr<IXpsOMPage> page; checked(d->at(index).reference->GetPage(&page));
        d->rasterizer.Reset();
        { Unlocked unlock;
          checked(d->factory->CreateRasterizer(page.Get(),x,y,
            XPSRAS_RENDERING_MODE_ANTIALIASED,XPSRAS_RENDERING_MODE_ANTIALIASED,
            XPSRAS_PIXEL_FORMAT_128BPP_PRGBA_FLOAT_SCRGB,
            XPSRAS_BACKGROUND_COLOR_TRANSPARENT,&d->rasterizer)); }
        Py_RETURN_NONE;
    } catch (Failure e) { return error(e.hr); }
}
PyObject* render_rect(PyObject*,PyObject* args) {
    PyObject* object; PyObject* target; PyObject* callback=Py_None; int x,y,w,h;
    if (!PyArg_ParseTuple(args,"OiiiiO|O",&object,&x,&y,&w,&h,&target,&callback)) return nullptr;
    auto d=document(object); if (!d) return nullptr;
    const uint64_t stride=static_cast<uint64_t>(w)*16;
    const uint64_t bytes=stride*static_cast<uint64_t>(h);
    if (x<0||y<0||w<=0||h<=0||stride>UINT_MAX||bytes>UINT_MAX||!d->rasterizer)
        return error(E_INVALIDARG);
    Py_buffer buffer{};
    if (PyObject_GetBuffer(target,&buffer,PyBUF_WRITABLE|PyBUF_C_CONTIGUOUS)<0) return nullptr;
    if (static_cast<uint64_t>(buffer.len)!=bytes) {
        PyBuffer_Release(&buffer); return error(E_INVALIDARG);
    }
    ComPtr<Notification> notification;
    if (callback!=Py_None) {
        if (!PyCallable_Check(callback)) { PyBuffer_Release(&buffer);PyErr_SetString(PyExc_TypeError,"cancel_check must be callable");return nullptr; }
        try { notification.Attach(new Notification(callback)); }
        catch (std::bad_alloc&) { PyBuffer_Release(&buffer);return PyErr_NoMemory(); }
    }
    HRESULT hr=S_OK;
    { Unlocked unlock;
      ComPtr<IWICBitmap> bitmap;
      hr=d->rasterizer->RasterizeRect(x,y,w,h,notification.Get(),&bitmap);
      if (SUCCEEDED(hr)) {
        WICPixelFormatGUID format{}; hr=bitmap->GetPixelFormat(&format);
        if (SUCCEEDED(hr) && format!=GUID_WICPixelFormat128bppPRGBAFloat) hr=E_UNEXPECTED;
        if (SUCCEEDED(hr)) hr=bitmap->CopyPixels(nullptr,static_cast<UINT>(stride),
            static_cast<UINT>(bytes),static_cast<BYTE*>(buffer.buf));
      }
    }
    PyBuffer_Release(&buffer);
    if (notification && notification->restore_error()) return nullptr;
    if (FAILED(hr)) return error(hr);
    Py_RETURN_NONE;
}
PyMethodDef methods[]={
    {"open",open_document,METH_O,nullptr},{"close",close_document,METH_O,nullptr},
    {"page_count",count_pages,METH_O,nullptr},{"page_info",describe_page,METH_VARARGS,nullptr},
    {"begin_page",begin_page,METH_VARARGS,nullptr},{"render_rect",render_rect,METH_VARARGS,nullptr},
    {nullptr,nullptr,0,nullptr}};
PyModuleDef module={PyModuleDef_HEAD_INIT,"_windows_xps_native",nullptr,-1,methods};
}
PyMODINIT_FUNC PyInit__windows_xps_native() { return PyModule_Create(&module); }
