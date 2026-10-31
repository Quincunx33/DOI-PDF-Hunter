@echo off
setlocal
cd /d "%~dp0"
set PDF_NO_PAUSE=1
python xtract_doi.py
echo.
pause
