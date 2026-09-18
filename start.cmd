@echo off
setlocal
cd /d "%~dp0"
if "%V2R_HOME%"=="" set V2R_HOME=%cd%
py -m app.v2r %*
