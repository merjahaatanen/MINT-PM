@echo off
REM ============================================================
REM  Launch Chrome with remote debugging enabled.
REM  This lets the Python scraper attach to (control) THIS
REM  browser window while keeping your existing login session.
REM
REM  HOW TO USE:
REM   1. Close ALL existing Chrome windows first (important!)
REM   2. Double-click this file.
REM   3. A Chrome window opens - log in if needed and go to:
REM      https://circaweb.bobrick.com/PME/Forms/EquipmentAll
REM   4. Run:  python scraper.py
REM ============================================================

set CHROME_PATH="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME_PATH% set CHROME_PATH="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

REM Use a dedicated debug profile folder so it never clashes with a
REM normally-running Chrome. You only need to log in here once.
set DEBUG_PROFILE="%LOCALAPPDATA%\Google\Chrome\PM_Debug_Profile"

start "" %CHROME_PATH% --remote-debugging-port=9222 --user-data-dir=%DEBUG_PROFILE% "https://circaweb.bobrick.com/PME/Forms/EquipmentAll"
