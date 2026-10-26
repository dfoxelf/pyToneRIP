// Copyright (C) 2026 Alexander Eichhorn
// SPDX-License-Identifier: GPL-3.0-only
// Typed SDK bindings; no private entry points or manually indexed COM vtables.
//
// RU: Этот модуль связывает Python с системным растеризатором XPS Windows.
// cython_setup.py собирает его как _windows_xps_native.*.pyd; windows_xps.py
// импортирует модуль и вызывает open -> page_info -> begin_page -> render_rect
// для каждой полосы -> close. Windows формирует RGBA float32 scRGB, а перевод
// в Gray16/RGB16 и запись TIFF выполняются далее в Cython/Python, не здесь.
// EN: This module connects Python to the Windows system XPS rasterizer.
// cython_setup.py builds _windows_xps_native.*.pyd; windows_xps.py imports it
// and calls open -> page_info -> begin_page -> render_rect for each band ->
// close. Windows produces RGBA float32 scRGB; subsequent Cython/Python code
// converts it to Gray16/RGB16 and writes TIFF. This module does not do that.
//
// RU: #include подключает объявления SDK при компиляции, но ничего не вызывает.
// ComPtr автоматически вызывает Release для COM-объектов. PyObject* относится
// к Python: счётчик ссылок управляется явно, а доступ требует GIL (блокировки
// интерпретатора). Исключения C++ преобразуются в ошибки Python на границе API.
// EN: #include makes SDK declarations available at compile time; it executes
// nothing. ComPtr automatically calls Release on COM objects. PyObject* belongs
// to Python: references are managed explicitly and access requires the GIL
// (interpreter lock). C++ failures are translated at the Python API boundary.
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
// RU: Проверяет HRESULT вызова Windows; при ошибке сохраняет исходный код
// в исключении Failure. Успешные S_OK/S_FALSE не считаются ошибками.
// EN: Check a Windows HRESULT; throw Failure with the original error code on
// failure. Success codes, including S_OK and S_FALSE, are accepted.
void checked(HRESULT hr) { if (FAILED(hr)) throw Failure{hr}; }
// RU: Область освобождения GIL вокруг длительного системного вызова.
// Сохраняет состояние текущего Python-потока при создании и восстанавливает
// его в деструкторе, в том числе при выходе через исключение C++.
// EN: Scoped GIL release around a long system call. Save the current Python
// thread state on construction; restore it on destruction, including unwinding
// after a C++ exception. Python objects must not be accessed inside this scope.
struct Unlocked {
    PyThreadState* state = PyEval_SaveThread();
    // RU: Снова захватывает GIL до продолжения работы с Python API.
    // EN: Reacquire the GIL before subsequent Python API operations.
    ~Unlocked() { PyEval_RestoreThread(state); }
};
// RU: COM callback отмены: Windows периодически вызывает Continue во время
// растеризации. Он обращается к Python cancel_check и сохраняет исключение,
// чтобы вернуть его вызывающему Python-коду после завершения системного вызова.
// EN: COM cancellation callback: Windows periodically calls Continue during
// rasterization. It invokes Python cancel_check and retains any exception for
// propagation to the Python caller after the system call returns.
class Notification final : public IXpsRasterizerNotificationCallback {
    std::atomic<ULONG> refs{1};
    PyObject* callable;
    PyObject *type=nullptr, *value=nullptr, *traceback=nullptr;
public:
    // RU: Удерживает Python callback собственным счётчиком ссылки; вход с GIL.
    // EN: Keep an owned reference to the Python callback; caller holds the GIL.
    explicit Notification(PyObject* callback): callable(callback) { Py_INCREF(callable); }
    // RU: Освобождает callback и сохранённое исключение под GIL, поскольку
    // последний COM Release может быть вызван системным кодом без этой блокировки.
    // EN: Release callback and saved exception under the GIL: the final COM
    // Release may originate from system code that does not hold the lock.
    ~Notification() {
        auto state=PyGILState_Ensure();
        Py_DECREF(callable);Py_XDECREF(type);Py_XDECREF(value);Py_XDECREF(traceback);
        PyGILState_Release(state);
    }
    // RU: Стандартный COM-запрос интерфейса. Поддерживает только IUnknown и
    // IXpsRasterizerNotificationCallback; успешный ответ получает свою ссылку.
    // EN: Standard COM interface query. Support only IUnknown and the rasterizer
    // notification interface; return an additional owned reference on success.
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out=nullptr;
        if (iid!=__uuidof(IUnknown) && iid!=__uuidof(IXpsRasterizerNotificationCallback)) return E_NOINTERFACE;
        *out=static_cast<IXpsRasterizerNotificationCallback*>(this);AddRef();return S_OK;
    }
    // RU: Атомарно удерживает COM callback ещё одним владельцем.
    // EN: Atomically retain the COM callback for one additional owner.
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    // RU: Атомарно снимает ссылку; последний владелец уничтожает объект.
    // EN: Atomically release a reference; the last owner destroys the object.
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG n=--refs;if (!n) delete this;return n;
    }
    // RU: Вызывается Windows, пока внешний render_rect работает без GIL.
    // Захватывает GIL для cancel_check(). Любое исключение означает остановку:
    // Windows получает ERROR_CANCELLED, а исходная ошибка сохраняется отдельно.
    // EN: Called by Windows while outer render_rect has released the GIL.
    // Acquire it to call cancel_check(). Any exception requests cancellation:
    // Windows receives ERROR_CANCELLED; the original exception is saved separately.
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
    // RU: После RasterizeRect возвращает сохранённое Python-исключение вместе
    // с traceback. Передаёт владение в PyErr_Restore и обнуляет свои ссылки,
    // чтобы деструктор не освободил их повторно. Вызывается с GIL.
    // EN: After RasterizeRect, restore the original Python exception/traceback.
    // Transfer ownership to PyErr_Restore and clear our references to avoid
    // releasing them twice. The caller must hold the GIL.
    bool restore_error() {
        if (!type) return false;
        PyErr_Restore(type,value,traceback);type=value=traceback=nullptr;return true;
    }
};
// Registered system class, tested at runtime. Its standalone activation is
// not promised by the XPSDrv documentation; failures are explicit, never 8-bit fallback.
const CLSID RasterFactory = {0x503e79bf,0x1d09,0x4764,{0x9d,0x72,0x1e,0xb0,0xc6,0x59,0x67,0xc6}};
// RU: Ссылка на страницу и её документ; документ нужен для PrintTicket уровня
// документа. Сохраняем COM-ссылки, а не растр всех страниц в памяти.
// EN: Page reference plus its owning document, needed for document-level
// PrintTickets. Retain COM references, not raster images for all pages.
struct Page {
    ComPtr<IXpsOMPageReference> reference;
    ComPtr<IXpsOMDocument> document;
};
// RU: Одна открытая XPS-сессия. Создание, использование и явное закрытие должны
// выполняться в одном потоке. Python-владелец — именованная капсула PyCapsule;
// normal path в windows_xps.py закрывает её в finally на том же потоке.
// EN: One open XPS session. Creation, use, and explicit close must stay on the
// same thread. An identified PyCapsule owns it; the normal windows_xps.py path
// closes the capsule in finally on that same thread.
struct Document {
    DWORD thread = GetCurrentThreadId();
    bool apartment = false;
    ComPtr<IXpsOMPackage1> package;
    ComPtr<IXpsOMDocumentSequence> sequence;
    ComPtr<IXpsRasterizationFactory2> factory;
    ComPtr<IXpsRasterizer> rasterizer;
    std::vector<Page> pages;
    // RU: Сначала освобождает COM-ссылки, затем балансирует собственный успешный
    // CoInitializeEx. Не завершает COM apartment, который не инициализировал.
    // EN: Release COM references before balancing our successful CoInitializeEx.
    // Do not uninitialize a COM apartment that this object did not initialize.
    ~Document() {
        rasterizer.Reset(); pages.clear(); factory.Reset(); sequence.Reset(); package.Reset();
        if (apartment) CoUninitialize();
    }
    // RU: Открывает XPS/OXPS по UTF-16 пути: инициализирует COM, создаёт объектную
    // модель XPS и фабрику растеризации, собирает плоский список страниц из всех
    // документов. При RPC_E_CHANGED_MODE использует уже установленный apartment.
    // Пустой пакет отклоняется; растеризация на этом этапе ещё не выполняется.
    // EN: Open XPS/OXPS from a UTF-16 path: initialize COM, create the XPS object
    // model and rasterization factory, then flatten pages from all documents.
    // RPC_E_CHANGED_MODE means an existing apartment is used without owning its
    // initialization. Reject an empty package; no pixels are rendered yet.
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
    // RU: Возвращает страницу по индексу с проверкой обеих границ до доступа
    // к vector. Отрицательный индекс и выход за конец дают E_INVALIDARG.
    // EN: Return a page by index, checking both bounds before vector access.
    // Negative and out-of-range indices produce E_INVALIDARG.
    Page& at(Py_ssize_t i) {
        if (i<0 || static_cast<size_t>(i)>=pages.size()) throw Failure{E_INVALIDARG};
        return pages[static_cast<size_t>(i)];
    }
};
// RU: Извлекает указатель из открытой капсулы с правильным именем и проверяет
// поток-владелец. Чужая/закрытая капсула или другой поток возвращают nullptr
// с установленной ошибкой Python; вызывающая функция должна её передать дальше.
// EN: Extract the pointer from a correctly named open capsule and check thread
// ownership. Invalid/closed capsules or another thread return nullptr with a
// Python exception set; the caller must propagate that error.
Document* document(PyObject* object) {
    auto d=static_cast<Document*>(PyCapsule_GetPointer(object,"pytone.xps.document"));
    if (d && d->thread!=GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError,"XPS session must stay on its creating thread");
        return nullptr;
    }
    return d;
}
// RU: Резервный деструктор капсулы, если Python-владелец не вызвал close явно.
// Удаляет Document только у ещё открытой капсулы. Не заменяет требование
// закрывать сессию в finally на создающем потоке.
// EN: Fallback capsule destructor if its Python owner did not call close.
// Delete Document only for an open capsule. This does not replace the contract
// to close the session in finally on its creating thread.
void destroy(PyObject* object) {
    if (PyCapsule_IsValid(object,"pytone.xps.document"))
        delete static_cast<Document*>(PyCapsule_GetPointer(object,"pytone.xps.document"));
}
// RU: Преобразует HRESULT в Python OSError с исходным шестнадцатеричным кодом.
// nullptr — соглашение Python C API: функция завершилась исключением.
// EN: Convert HRESULT to Python OSError with the original hexadecimal code.
// Returning nullptr signals an exception according to the Python C API.
PyObject* error(HRESULT hr) {
    PyErr_Format(PyExc_OSError,"Windows high-precision XPS renderer failed (HRESULT 0x%08x)",
                 static_cast<unsigned int>(hr));
    return nullptr;
}
// RU: Читает XML PrintTicket из COM IStream и возвращает Python bytes без
// интерпретации XML. Отсутствующий ресурс даёт b""; размер ограничен 16 МиБ,
// короткое чтение считается ошибкой. Разбор и наследование тикетов — в Python.
// EN: Read PrintTicket XML from its COM IStream into Python bytes without
// interpreting XML. An absent resource gives b""; size is capped at 16 MiB
// and a short read is an error. Python resolves XML and ticket inheritance.
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
// RU: Python open(filename: str) -> capsule. Переводит путь в UTF-16, открывает
// Document без GIL и передаёт владение капсуле. При сбое освобождает путь и
// частично созданную сессию; ошибки Windows/памяти переводит в исключения Python.
// EN: Python open(filename: str) -> capsule. Convert path to UTF-16, initialize
// Document without the GIL, and transfer ownership to a capsule. On failure,
// free path and partial session; translate Windows/allocation errors to Python.
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
// RU: Python close(capsule) -> None. Проверяет поток, удаляет сессию и помечает
// капсулу закрытой, отключая её деструктор: повторного delete при сборке мусора
// не будет. Повторный close не является успешной операцией.
// EN: Python close(capsule) -> None. Check thread, delete session, disable the
// capsule destructor and mark it closed to prevent a second delete during GC.
// A repeated close is rejected rather than treated as a successful operation.
PyObject* close_document(PyObject*,PyObject* object) {
    auto d=document(object); if (!d) return nullptr;
    delete d; PyCapsule_SetDestructor(object,nullptr);
    PyCapsule_SetName(object,"pytone.xps.closed"); Py_RETURN_NONE;
}
// RU: Python page_count(capsule) -> int. Возвращает число страниц всех документов
// в пакете после проверки капсулы/потока; изображение для этого не формируется.
// EN: Python page_count(capsule) -> int. After capsule/thread checks, return the
// total number of pages across package documents, without rendering an image.
PyObject* count_pages(PyObject*,PyObject* object) {
    auto d=document(object); return d?PyLong_FromSize_t(d->pages.size()):nullptr;
}
// RU: Python page_info(capsule, index) -> (width, height, sequence_ticket,
// document_ticket, page_ticket). Размеры в единицах XPS (1/96 дюйма), не в
// пикселях выбранного DPI; три тикета — bytes для дальнейшего объединения в
// Python. Формат N в Py_BuildValue передаёт владение ссылками в результирующий tuple.
// EN: Python page_info(capsule, index) returns (width, height, sequence_ticket,
// document_ticket, page_ticket). Dimensions use XPS units (1/96 inch), not
// pixels at the requested DPI. The three tickets are bytes for Python to merge.
// Py_BuildValue's N format transfers their references into the returned tuple.
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
// RU: Python begin_page(capsule, index, dpi_x, dpi_y) -> None. Проверяет DPI,
// освобождает предыдущий растеризатор и создаёт новый для выбранной страницы.
// Запрашивает сглаживание, прозрачный фон и 128 бит на пиксель: четыре float32
// RGBA в линейном scRGB, RGB уже умножены на alpha (premultiplied).
// EN: Python begin_page(capsule, index, dpi_x, dpi_y) -> None. Validate DPI,
// release the previous rasterizer and create one for the selected page.
// Request antialiasing, transparent background and 128 bits per pixel: four
// float32 RGBA values in linear scRGB, with RGB premultiplied by alpha.
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
// RU: Python render_rect(capsule, x, y, width, height, target, cancel_check=None).
// Координаты и размеры заданы в пикселях сетки begin_page. target — заранее
// выделенный записываемый непрерывный буфер, ровно width*height*16 байт. Проверки
// длины/диапазона выполняются до передачи указателя в Windows; Py_buffer
// удерживает буфер до конца вызова. Без GIL Windows растеризует прямоугольник,
// затем CopyPixels копирует его в target. Формат результата проверяется явно.
// При отмене восстанавливается исходное Python-исключение, иначе — ошибка HRESULT.
// EN: Python render_rect(capsule, x, y, width, height, target, cancel_check=None).
// Coordinates/dimensions use the pixel grid selected by begin_page. target is
// a preallocated writable contiguous buffer of exactly width*height*16 bytes.
// Check size/range before passing its pointer to Windows; Py_buffer retains
// the buffer for the call. With the GIL released, Windows rasterizes the region
// and CopyPixels copies it into target. Explicitly verify the returned format.
// On cancellation restore the Python exception; otherwise report HRESULT failure.
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
// RU: Экспортируемые имена Python -> реализации выше. METH_O принимает один
// объект, METH_VARARGS — позиционные аргументы в tuple; nullptr завершает таблицу.
// EN: Exported Python names -> implementations above. METH_O accepts one object,
// METH_VARARGS accepts positional arguments as a tuple; nullptr ends the table.
PyMethodDef methods[]={
    {"open",open_document,METH_O,nullptr},{"close",close_document,METH_O,nullptr},
    {"page_count",count_pages,METH_O,nullptr},{"page_info",describe_page,METH_VARARGS,nullptr},
    {"begin_page",begin_page,METH_VARARGS,nullptr},{"render_rect",render_rect,METH_VARARGS,nullptr},
    {nullptr,nullptr,0,nullptr}};
// RU: Описание расширения для CPython; -1 означает отсутствие отдельного
// состояния на каждый экземпляр модуля. Сами документы живут в капсулах.
// EN: CPython extension descriptor; -1 means no per-module state allocation.
// Individual document sessions are owned by capsules.
PyModuleDef module={PyModuleDef_HEAD_INIT,"_windows_xps_native",nullptr,-1,methods};
}
// RU: Точка входа загрузчика CPython при import _windows_xps_native из .pyd.
// Создаёт объект модуля с таблицей methods; XPS здесь ещё не открывается.
// EN: CPython loader entry point when importing _windows_xps_native from .pyd.
// Create the module with its methods table; no XPS document is opened here.
PyMODINIT_FUNC PyInit__windows_xps_native() { return PyModule_Create(&module); }
