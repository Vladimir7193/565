@echo off
echo ============================================
echo   DepositBooster - Paper Trading Bot
echo ============================================
echo.
echo Starting trading bot in background...
start "DepositBooster Bot" cmd /k "cd /d %~dp0 && python bot.py"
timeout /t 3 /nobreak >nul
echo Starting dashboard...
start "DepositBooster Dashboard" cmd /k "cd /d %~dp0 && streamlit run dashboard.py --server.port 8503"
timeout /t 4 /nobreak >nul
start "" "http://localhost:8503"
echo.
echo Bot:       running in background window
echo Dashboard: http://localhost:8503
echo.
pause
