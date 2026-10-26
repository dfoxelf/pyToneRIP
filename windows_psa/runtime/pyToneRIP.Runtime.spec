# -*- mode: python ; coding: utf-8 -*-

from importlib.metadata import distribution
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

runtime_dir = Path(SPEC).resolve().parent
project_root = runtime_dir.parents[1]
source_root = project_root / "src"

# Omit optional formats outside the supported input path. JPEG XS, Jetraw
# and HEIF also require external libraries not shipped with imagecodecs.
# Standard TIFF compression codecs remain available; TIFF pages using the
# excluded codecs are not supported.
excluded_codecs = {
    "imagecodecs._avif",
    "imagecodecs._heif",
    "imagecodecs._jetraw",
    "imagecodecs._jpegxl",
    "imagecodecs._jpegxs",
}


def license_file(distribution_name, suffix):
    package = distribution(distribution_name)
    relative_name = next(
        item for item in package.files or () if item.as_posix().endswith(suffix)
    )
    item = package.locate_file(relative_name)
    return (str(item), f"licenses/{distribution_name}")


datas = [
    (str(source_root / "xp750_rip" / "dither" / "data"), "xp750_rip/dither/data"),
    (str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/Python"),
    license_file("numpy", ".dist-info/licenses/LICENSE.txt"),
    license_file("pillow", ".dist-info/licenses/LICENSE"),
    license_file("tifffile", ".dist-info/licenses/LICENSE"),
    license_file("imagecodecs", ".dist-info/licenses/LICENSE"),
]
datas += collect_data_files("imagecodecs", includes=["licenses/*"])
catalog_root = project_root / "printer_catalog"
if catalog_root.is_dir():
    # Catalog packages are self-contained runtime descriptors. The installed
    # executable must never need the development knowledge workspace.
    datas.append((str(catalog_root), "xp750_rip/printers/catalog_data"))

a = Analysis(
    [str(source_root / "xp750_rip" / "runtime_host.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    # imagecodecs loads its native codecs lazily; static import analysis alone
    # misses the decoders used by compressed 16-bit TIFF pages.
    hiddenimports=["xp750_rip._cython_kernels", "xp750_rip._windows_xps_native"] + collect_submodules(
        "imagecodecs", filter=lambda name: name not in excluded_codecs
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tifffile's optional file dialogs and Pillow's Tk bridge are unused by
    # the RIP. Exclude both Python wrappers and native bridges so their hooks
    # cannot collect the Tcl/Tk DLLs or runtime initialization code.
    # Pillow has its own AVIF decoder, independent of imagecodecs._avif;
    # exclude both that plugin and its native module as well.
    excludes=["scipy", "pytest", "tkinter", "_tkinter", "PIL.ImageTk", "PIL._imagingtk",
              "PIL.AvifImagePlugin", "PIL._avif"]
    + sorted(excluded_codecs),
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pyToneRIP.Runtime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="x86_64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pyToneRIP.Runtime",
)
