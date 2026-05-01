# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import importlib
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(globals().get('SPECPATH', '.')).resolve()


def safe_collect_submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []


def safe_collect_data_files(package):
    try:
        return collect_data_files(package)
    except Exception:
        return []


def safe_copy_metadata(package):
    try:
        return copy_metadata(package)
    except Exception:
        return []


def package_importable(package):
    try:
        importlib.import_module(package)
        return True
    except Exception:
        return False


hiddenimports = []
required_packages = [
    'src',
    'playwright',
    'patchright',
    'httpx',
    'rich',
    'cryptography',
    'pyotp',
]
optional_packages = [
    'playwright_stealth',
]

for package in required_packages:
    hiddenimports += safe_collect_submodules(package)
for package in optional_packages:
    if package_importable(package):
        hiddenimports += safe_collect_submodules(package)

datas = []
for package in ['playwright', 'patchright']:
    datas += safe_collect_data_files(package)
    datas += safe_copy_metadata(package)
for package in optional_packages:
    if package_importable(package):
        datas += safe_collect_data_files(package)
        datas += safe_copy_metadata(package)

a = Analysis(
    ['src\\worker_api.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='worker_api',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
