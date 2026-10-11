# Build Harvest.exe on a Windows PC.
#   cd harvest
#   .\desktop\build_windows.ps1
# Output: dist\Harvest.exe
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m venv .buildenv
.\.buildenv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r desktop\requirements.txt pyinstaller

python desktop\make_icon.py

pyinstaller --noconfirm --clean --onefile --windowed `
  --name Harvest `
  --icon desktop\harvest.ico `
  --paths . `
  --collect-submodules pystray `
  desktop\harvest_tray.py

Write-Host "`nBuilt dist\Harvest.exe"
