@echo off
chcp 65001 >nul
title Golos 2.0 — Обновление
cd /d "%~dp0"

echo.
echo  Скачиваю обновления с GitHub...
echo.
git pull
if errorlevel 1 (
    echo.
    echo  ОШИБКА при обновлении. Скопируй текст ошибки выше и покажи Клоду.
    pause
    exit /b 1
)

echo.
findstr /C:"DEEPGRAM_ADMIN_KEY" .env >nul 2>&1
if errorlevel 1 (
    echo  В .env ещё нет ключа для чтения реального баланса Deepgram.
    echo  Если он у тебя уже есть — вставь его ниже и нажми Enter.
    echo  Если ещё не создавал — просто нажми Enter, программа продолжит
    echo  работать как раньше ^(с оценкой вместо реального баланса^).
    echo.
    set /p ADMINKEY="Admin-ключ Deepgram: "
    if not "%ADMINKEY%"=="" (
        echo DEEPGRAM_ADMIN_KEY=%ADMINKEY%>> .env
        echo  Ключ добавлен в .env.
    )
) else (
    echo  Ключ DEEPGRAM_ADMIN_KEY уже есть в .env — пропускаю.
)

echo.
echo  Готово! Закрой Golos 2.0 ^(если запущена^) и открой заново через
echo  ярлык "Golos 2.0" на рабочем столе.
echo.
pause
