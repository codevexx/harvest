# PyInstaller recipe for Harvest.app (macOS menu bar app). Build from this folder:
#   python make_mac_icons.py && pyinstaller --noconfirm Harvest.spec
import os
import sys

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
sys.path.insert(0, os.path.join(root, "desktop"))
from version import __version__

a = Analysis(
    ["harvest_mac.py"],
    pathex=[root, os.path.join(root, "desktop")],
    datas=[("menubar.png", ".")],
    hiddenimports=["worker.agent", "worker.runner", "worker.idle", "worker.runtimes", "worker.browser_harness"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Harvest", console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Harvest")
app = BUNDLE(
    coll,
    name="Harvest.app",
    icon="Harvest.icns",
    bundle_identifier="com.codevexx.harvest",
    info_plist={
        "LSUIElement": True,  # menu bar only, no Dock icon
        "CFBundleName": "Harvest",
        "CFBundleDisplayName": "Harvest",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Copyright 2026 codevexx. Apache License 2.0.",
    },
)
