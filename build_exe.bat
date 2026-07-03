@echo off
title Compilando Logibot Picking Pro...
color 0A
echo.
echo  ██╗      ██████╗  ██████╗ ██╗██████╗  ██████╗ ████████╗
echo  ██║     ██╔═══██╗██╔════╝ ██║██╔══██╗██╔═══██╗╚══██╔══╝
echo  ██║     ██║   ██║██║  ███╗██║██████╔╝██║   ██║   ██║   
echo  ██║     ██║   ██║██║   ██║██║██╔══██╗██║   ██║   ██║   
echo  ███████╗╚██████╔╝╚██████╔╝██║██████╔╝╚██████╔╝   ██║   
echo  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝╚═════╝  ╚═════╝   ╚═╝   
echo.
echo  ============================================
echo   Compilador de Logibot Picking Pro v2.1.0
echo  ============================================
echo.

:: ── Verificar que Python y PyInstaller estén instalados ──────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no está instalado o no está en el PATH.
    echo         Descargá Python desde https://python.org
    pause & exit /b 1
)

pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] Instalando PyInstaller...
    pip install pyinstaller
)

pip show pillow >nul 2>&1
if errorlevel 1 (
    echo [INFO] Instalando Pillow...
    pip install Pillow
)

pip show pymupdf >nul 2>&1
if errorlevel 1 (
    echo [INFO] Instalando PyMuPDF...
    pip install PyMuPDF
)

:: ── Directorio del script ─────────────────────────────────────────────────────
cd /d "%~dp0"

echo [1/4] Limpiando compilaciones anteriores...
if exist "dist\Logibot" rmdir /S /Q "dist\Logibot"
if exist "build" rmdir /S /Q "build"

echo [2/4] Compilando con PyInstaller...
echo.

pyinstaller ^
    --name "Logibot" ^
    --onedir ^
    --windowed ^
    --icon "logibot_icon.ico" ^
    --add-data "templates/movil.html;templates" ^
    --add-data "logibot_updater.py;." ^
    --hidden-import "PIL._tkinter_finder" ^
    --hidden-import "fitz" ^
    --hidden-import "reportlab.pdfgen" ^
    --hidden-import "reportlab.lib" ^
    --hidden-import "pypdf" ^
    --hidden-import "openpyxl" ^
    --hidden-import "tkinter" ^
    --hidden-import "tkinter.ttk" ^
    --hidden-import "tkinter.filedialog" ^
    --hidden-import "tkinter.messagebox" ^
    --collect-all "reportlab" ^
    --collect-all "fitz" ^
    --noconfirm ^
    app_deposito.py

if errorlevel 1 (
    echo.
    echo [ERROR] La compilacion falló. Revisá los mensajes de arriba.
    pause & exit /b 1
)

echo.
echo [3/4] Copiando archivos de datos (se conservan entre versiones)...

:: Crear estructura de la distribución
set DIST_DIR=dist\Logibot

:: El config.json, excel_cache.json y logo se copian solo si existen
:: y si NO hay uno ya en la carpeta destino (para preservar datos del usuario)
if exist "config.json" (
    if not exist "%DIST_DIR%\config.json" (
        copy /Y "config.json" "%DIST_DIR%\config.json" >nul
        echo   config.json copiado
    ) else (
        echo   config.json existente conservado
    )
)

if exist "excel_cache.json" (
    if not exist "%DIST_DIR%\excel_cache.json" (
        copy /Y "excel_cache.json" "%DIST_DIR%\excel_cache.json" >nul
        echo   excel_cache.json copiado
    ) else (
        echo   excel_cache.json existente conservado
    )
)

if exist "logo_cache.png" (
    copy /Y "logo_cache.png" "%DIST_DIR%\logo_cache.png" >nul
    echo   logo_cache.png copiado
)

:: Crear archivo de versión para el updater
echo {"version":"2.1.0","compilado":"%DATE% %TIME%"} > "%DIST_DIR%\version_local.json"

echo [4/4] Compilación terminada.
echo.
echo  ✅ Logibot.exe generado en:  %DIST_DIR%\Logibot.exe
echo.
echo  Para distribuir, comprimí toda la carpeta dist\Logibot\ en un .zip
echo  Los archivos de datos (config.json, excel_cache.json) se conservan
echo  entre actualizaciones porque están en la misma carpeta que el .exe
echo.

:: Abrir la carpeta de salida
explorer "%~dp0dist\Logibot"

pause
