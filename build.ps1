$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
py -m pip install --upgrade pip
py -m pip install ".[build,test]"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
py -m playwright install chromium
py -m pytest
py -m PyInstaller --noconfirm --clean "v2r-unified-control.spec"

Write-Host ""
Write-Host "빌드 완료: dist\V2R-Playwright-Web.exe"
