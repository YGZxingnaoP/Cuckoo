# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.'), ('Radmin', 'Radmin'), ('runtime/libvlc.dll', 'vlc'), ('runtime/libvlccore.dll', 'vlc'), ('runtime/plugins', 'vlc/plugins')],
    hiddenimports=['soundcard', 'soundcard._soundcard', 'dxcam', 'comtypes', 'comtypes.stream', 'turbojpeg', 'cv2', 'numpy', 'pyaudio', 'vlc', 'func.cinema', 'func.cinema.cinema_host', 'func.cinema.cinema_guest'],
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
    name='Cuckoo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.png'],
)
