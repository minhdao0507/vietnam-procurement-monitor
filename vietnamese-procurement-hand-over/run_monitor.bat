@echo off
cd /d "M:\Working\Apple\apple_monitor"
set PYTHON=C:\Users\Laptop\AppData\Local\Programs\Python\Python312\python.exe
set LOGFILE=M:\Working\Apple\apple_monitor\monitor.log
echo [%date% %time%] Starting monitor >> "%LOGFILE%"
"%PYTHON%" run_monitor.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] Exit code: %ERRORLEVEL% >> "%LOGFILE%"
