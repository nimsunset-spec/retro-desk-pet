from pathlib import Path


ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "assets" / "sprites"), "assets/sprites"),
        (str(ROOT / "brain" / "dataset.json"), "brain"),
    ],
    hiddenimports=["pygetwindow"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy",
        "scipy",
        "sklearn",
        "torch",
        "torchvision",
        "torchaudio",
        "triton",
        "pandas",
        "matplotlib",
        "jupyter",
        "IPython",
        "brain.train",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RetroDeskPet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

app = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RetroDeskPet",
)
