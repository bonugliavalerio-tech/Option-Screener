@echo off
rem Starts the screener: a page opens in the browser.
rem First time: run install.bat
cd /d "%~dp0"
python -m streamlit run app.py
pause
