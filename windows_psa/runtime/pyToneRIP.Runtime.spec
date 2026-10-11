# -*- mode: python ; coding: utf-8 -*-

from importlib.metadata import distribution
from pathlib import Path
import sys

runtime_dir = Path(SPEC).resolve().parent
project_root = runtime_dir.parents[1]
source_root = project_root / "src"


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
]
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
    hiddenimports=["xp750_rip._cython_kernels"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["scipy", "pytest"],
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
