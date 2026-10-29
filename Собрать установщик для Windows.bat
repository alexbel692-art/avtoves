@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Сборка установщика АвтоВес
echo Сборка установщика АвтоВес (нужен интернет, 5-15 минут)
echo.
set /p VER="Версия [1.0.0]: "
if "%VER%"=="" set VER=1.0.0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build\build.ps1" -Version %VER%
if errorlevel 1 (
  echo.
  echo Ошибка сборки. Пришлите текст выше.
  pause
  exit /b 1
)
echo.
echo Готово! Установщик в папке dist
start "" "%~dp0dist"
pause
