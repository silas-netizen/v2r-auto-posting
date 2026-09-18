$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

py -3.11 -m pip install -e ".[test]"
if ($LASTEXITCODE -ne 0) {
    py -m pip install -e ".[test]"
}

if (-not (Test-Path "$root\config\config.json")) {
    Copy-Item "$root\config\config.example.json" "$root\config\config.json"
}

New-Item -ItemType Directory -Force -Path "$root\data", "$root\logs", "$root\media-cache", "$root\browser-profile" | Out-Null
Write-Host "V2R 내부 명령 시스템을 설치했습니다. start.cmd 로 실행하세요."
