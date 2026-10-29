@echo off
rem Run once: installs the libraries.
cd /d "%~dp0"
python -m pip install -r requirements.txt
pause
