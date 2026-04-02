@echo off
chcp 65001 >nul
title Golos 2.0 — Установка

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║         GOLOS 2.0 — УСТАНОВКА              ║
echo  ║     Голосовой ввод текста для Windows     ║
echo  ╚══════════════════════════════════════════╝
echo.

:: Проверяем Python
echo  [1/4] Проверяю Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ОШИБКА: Python не найден!
    echo  Скачайте Python с https://python.org
    echo  При установке обязательно поставьте галочку "Add to PATH"
    echo.
    pause
    exit /b 1
)
python --version
echo         OK
echo.

:: Устанавливаем зависимости
echo  [2/4] Устанавливаю библиотеки...
pip install vosk sounddevice keyboard pyperclip pillow pystray numpy python-dotenv websockets --quiet
if errorlevel 1 (
    echo.
    echo  ОШИБКА при установке библиотек.
    echo  Попробуйте запустить от имени администратора.
    pause
    exit /b 1
)
echo         OK
echo.

:: Проверяем модель
echo  [3/4] Проверяю модель распознавания...
if exist "%~dp0model\am" (
    echo         Модель найдена — OK
) else (
    echo.
    echo  ОШИБКА: Папка model не найдена!
    echo  Убедитесь, что папка model находится рядом с этим файлом.
    echo.
    pause
    exit /b 1
)
echo.

:: Создаём ярлык на рабочем столе
echo  [4/4] Создаю ярлык на рабочем столе...
set "SCRIPT_DIR=%~dp0"
set "VBS=%TEMP%\golos2_shortcut.vbs"

echo Set ws = WScript.CreateObject("WScript.Shell") > "%VBS%"
echo Set shortcut = ws.CreateShortcut(ws.SpecialFolders("Desktop") ^& "\Golos 2.0.lnk") >> "%VBS%"
echo shortcut.TargetPath = "pythonw.exe" >> "%VBS%"
echo shortcut.Arguments = "%SCRIPT_DIR%voice_input.py" >> "%VBS%"
echo shortcut.WorkingDirectory = "%SCRIPT_DIR%" >> "%VBS%"
echo shortcut.Description = "Golos 2.0 — voice input (Alt+X)" >> "%VBS%"
echo shortcut.Save >> "%VBS%"
cscript //nologo "%VBS%"
del "%VBS%"
echo         Ярлык "Golos 2.0" создан на рабочем столе — OK
echo.

echo  ╔══════════════════════════════════════════╗
echo  ║       УСТАНОВКА ЗАВЕРШЕНА!               ║
echo  ║                                          ║
echo  ║  Запустите Golos 2.0:                      ║
echo  ║    - Ярлык "Golos 2.0" на рабочем столе    ║
echo  ║    - Или файл start.bat в этой папке     ║
echo  ║                                          ║
echo  ║  Горячая клавиша: Alt+X                  ║
echo  ╚══════════════════════════════════════════╝
echo.
pause
