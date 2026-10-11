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
  --paths . --paths desktop `
  --add-data "desktop/ui;ui" `
  --collect-submodules pystray `
  --collect-data webview `
  desktop\harvest_tray.py

# Installer (needs Inno Setup 6: https://jrsoftware.org/isinfo.php)
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
  $v = (python -c "import sys; sys.path.insert(0,'desktop'); import version; print(version.__version__)").Trim()
  & $iscc "/DAppVersion=$v" desktop\installer.iss
}

foreach ($f in Get-ChildItem dist\*.exe) {
  $h = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
  "$h  $($f.Name)" | Out-File -Encoding ascii -NoNewline "$($f.FullName).sha256"
}
Write-Host "`nBuilt:"; Get-ChildItem dist
