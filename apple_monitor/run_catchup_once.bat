@echo off
cd /d "M:\Working\Apple\apple_monitor"
set PYTHON=C:\Users\Laptop\AppData\Local\Programs\Python\Python312\python.exe

echo [%date% %time%] Step 1: Refresh token >> catchup.log 2>&1
"%PYTHON%" token_refresh.py >> catchup.log 2>&1

echo [%date% %time%] Step 2: Crawl new bids (no email) >> catchup.log 2>&1
"%PYTHON%" apple_monitor.py crawl >> catchup.log 2>&1

echo [%date% %time%] Step 3: Send catchup email >> catchup.log 2>&1
"%PYTHON%" send_catchup.py >> catchup.log 2>&1

echo [%date% %time%] Done >> catchup.log 2>&1
