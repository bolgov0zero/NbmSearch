@echo off
chcp 65001 >nul
title NbmSearch

echo ============================================
echo  NbmSearch — локальный поиск по файлам
echo ============================================
echo.

:: Проверка Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден.
    echo Скачайте Python 3.11+ с сайта: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: Установка зависимостей если нужно
if not exist ".deps_installed" (
    echo Устанавливаю зависимости...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить зависимости.
        pause
        exit /b 1
    )
    echo. > .deps_installed
    echo.
)

echo Запускаю сервер на http://localhost:8080
echo Для остановки закройте это окно.
echo.

:: Запуск сервера в фоне и открытие браузера
start "" /b python -m app.main

:: Ждём пока сервер поднимется
timeout /t 2 /nobreak >nul

:: Открываем браузер
start "" http://localhost:8080

:: Держим окно открытым — при закрытии процесс завершится
:loop
timeout /t 5 /nobreak >nul
tasklist /fi "imagename eq python.exe" /fo csv 2>nul | find /i "python.exe" >nul
if errorlevel 1 goto end
goto loop

:end
echo Сервер остановлен.
