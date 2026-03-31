@echo off
title Golos 2.0
echo.
echo  Golos 2.0 - Voice Input
echo  Hotkey: Alt+X
echo.
python "%~dp0voice_input.py"
if errorlevel 1 pause