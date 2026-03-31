@echo off
chcp 65001 >nul
title Golos 2 — Голосовой ввод

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║            GOLOS 2  v2.0                 ║
echo  ║     Голосовой ввод текста для Windows     ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  Горячая клавиша:  Alt+X
echo  Команды:          точка, запятая, вопрос,
echo                    восклицание, тире, удали...
echo  Полный список:    COMMANDS.md
echo.
echo  Закрыть: правый клик по иконке в трее - Выход
echo.
echo  ─────────────────────────────────────────────
echo.

python "%~dp0voice_input.py"

if errorlevel 1 (
    echo.
    echo  Произошла ошибка. Запустите install.bat
    echo.
    pause
)
