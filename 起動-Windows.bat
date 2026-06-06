@echo off
chcp 65001 >nul
title Clipboard Bridge
echo Clipboard Bridge を起動します...
echo.
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0clipboard_bridge.py"
) else (
    py "%~dp0clipboard_bridge.py"
)
echo.
echo 終了しました。ウィンドウを閉じてください。
pause
