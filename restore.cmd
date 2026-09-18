@echo off
setlocal
cd /d "%~dp0"
if "%V2R_HOME%"=="" set V2R_HOME=%cd%
if "%~1"=="" (
  echo 사용법: restore.cmd data\backup_YYYYMMDD_HHMMSS.sqlite
  exit /b 1
)
copy /y "%~1" "%V2R_HOME%\data\v2r.sqlite" >nul
echo 복원 완료: %V2R_HOME%\data\v2r.sqlite
