@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if defined STUDIO_PYTHON goto explicit
if exist ".venv\Scripts\python.exe" goto venv
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto launcher
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto python
python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto python3
echo Python 3.11+ not found. Install a compatible Python and reopen this window.
set result=1
goto finish
:explicit
"%STUDIO_PYTHON%" bootstrap.py %*
goto result
:venv
".venv\Scripts\python.exe" bootstrap.py %*
goto result
:launcher
py -3 bootstrap.py %*
goto result
:python
python bootstrap.py %*
goto result
:python3
python3 bootstrap.py %*
:result
set result=%errorlevel%
:finish
if not defined STUDIO_NO_PAUSE pause
exit /b %result%
