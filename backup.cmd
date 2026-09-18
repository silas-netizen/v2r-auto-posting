@echo off
setlocal
cd /d "%~dp0"
if "%V2R_HOME%"=="" set V2R_HOME=%cd%
if not exist "%V2R_HOME%\data\v2r.sqlite" (
  echo 백업할 SQLite 파일이 없습니다.
  exit /b 1
)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
set DEST=%V2R_HOME%\data\backup_%STAMP%.sqlite
copy /y "%V2R_HOME%\data\v2r.sqlite" "%DEST%" >nul
echo %DEST%
