@echo off
setlocal enabledelayedexpansion
:: ============================================================
:: run_daily.bat — 每日 AI 科技新知報告排程執行檔
:: 由 Windows 工作排程器呼叫，不需要手動執行
:: ============================================================

set PROJECT_DIR=D:\claude\AI_Agent\env_ai_reporter
set LOG_FILE=%PROJECT_DIR%\logs\run_daily.log
set PYTHON=python

:: 強制 Python 使用 UTF-8 輸出，避免 Windows cp950 無法編碼 emoji 導致 YouTube 抓取中斷
set PYTHONIOENCODING=utf-8

:: 建立 logs 目錄
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

:: Log rotation：超過 500KB 則清除舊紀錄，保留最近 200 行
if exist "%LOG_FILE%" (
    for %%A in ("%LOG_FILE%") do set LOG_SIZE=%%~zA
    if !LOG_SIZE! gtr 512000 (
        powershell -NoProfile -Command ^
            "$lines = Get-Content '%LOG_FILE%' -Encoding UTF8; $tail = $lines | Select-Object -Last 200; $tail | Set-Content '%LOG_FILE%' -Encoding UTF8"
    )
)

:: 紀錄執行時間
echo ============================================================ >> "%LOG_FILE%"
echo 執行時間：%date% %time% >> "%LOG_FILE%"
echo ============================================================ >> "%LOG_FILE%"

:: 切換到專案目錄並執行
cd /d "%PROJECT_DIR%"
%PYTHON% scripts\send_report.py >> "%LOG_FILE%" 2>&1

:: 記錄結束狀態
echo 結束碼：%errorlevel% >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"
