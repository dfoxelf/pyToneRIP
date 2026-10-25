# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""Build the x64 AOT Cython kernels in-place for development and PyInstaller."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from Cython.Build import cythonize
from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "src" / "xp750_rip" / "_cython_kernels.pyx"

if sys.platform == "win32":
    compile_args = ["/O2", "/openmp"]
    link_args: list[str] = []
else:
    compile_args = ["-O3", "-fopenmp"]
    link_args = ["-fopenmp"]

extension = Extension(
    "xp750_rip._cython_kernels",
    [str(SOURCE)],
    include_dirs=[np.get_include()],
    define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
    extra_compile_args=compile_args,
    extra_link_args=link_args,
)

extensions = [extension]
if sys.platform == "win32":
    extensions.append(Extension(
        "xp750_rip._windows_xps_native",
        [str(ROOT / "src" / "xp750_rip" / "_windows_xps_native.cpp")],
        language="c++", libraries=["ole32", "uuid", "windowscodecs"],
        extra_compile_args=["/O2", "/std:c++17", "/EHsc"],
    ))

setup(
    name="xp750-rip-cython-kernels",
    package_dir={"": "src"},
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,
            "wraparound": False,
            "initializedcheck": False,
            "cdivision": True,
        },
    ),
)
