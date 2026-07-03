@echo off
title Compilando Logibot...
color 0A

cd /d "%~dp0"

echo.
echo ============================================
echo  Logibot Picking Pro - Compilador
echo ============================================
echo.

:: Borrar spec viejos que pueden interferir
if exist "Logibot.spec" del /Q "Logibot.spec"
if exist "app_deposito.spec" del /Q "app_deposito.spec"

:: Borrar compilaciones anteriores
if exist "dist" rmdir /S /Q "dist"
if exist "build" rmdir /S /Q "build"

:: Generar icono si no existe
if not exist "logibot_icon.ico" (
    echo Generando icono...
    python -c "from PIL import Image,ImageDraw;import struct,io;img=Image.new('RGBA',(256,256),(0,0,0,0));d=ImageDraw.Draw(img);[d.rectangle([8+r,8,248-r,248],fill=(30,58,138,255)) for r in range(0,41)];d.rectangle([70,60,110,190],fill=(255,255,255,255));d.rectangle([70,160,180,200],fill=(255,255,255,255));d.ellipse([150,60,200,110],fill=(251,191,36,255));sizes=[(s,io.BytesIO()) for s in [16,32,48,64,128,256]];[img.resize((s,s),Image.Resampling.LANCZOS).save(b,'PNG') for s,b in sizes];f=open('logibot_icon.ico','wb');f.write(struct.pack('<HHH',0,1,len(sizes)));off=6+len(sizes)*16;[f.write(struct.pack('<BBBBHHII',0 if s==256 else s,0 if s==256 else s,0,0,1,32,len(b.getvalue()),off)) or setattr(type('',(),{}),'_',off:=off+len(b.getvalue())) for s,b in sizes];[f.write(b.getvalue()) for s,b in sizes];f.close();print('OK')"
)

echo Instalando PyInstaller...
pip install pyinstaller --quiet

echo.
echo Compilando (3-5 minutos, no cerres esta ventana)...
echo.

pyinstaller --name "Logibot" --onedir --windowed --icon "logibot_icon.ico" --add-data "templates/movil.html;templates" --add-data "logibot_updater.py;." --add-data "logibot_dashboard.py;." --add-data "logibot_preview_etiqueta.py;." --hidden-import "PIL._tkinter_finder" --hidden-import "fitz" --hidden-import "reportlab.pdfgen.canvas" --hidden-import "reportlab.lib.colors" --hidden-import "reportlab.lib.utils" --hidden-import "pypdf" --hidden-import "openpyxl" --collect-all "reportlab" --collect-all "fitz" --collect-all "PIL" --noconfirm app_deposito.py

if errorlevel 1 (
    echo.
    echo [ERROR] La compilacion fallo.
    echo Intentando ver el error...
    pause
    exit /b 1
)

echo.
echo Copiando archivos de datos...

:: Copiar datos si no existen ya en dist
for %%F in (config.json excel_cache.json metrics.json) do (
    if exist "%%F" if not exist "dist\Logibot\%%F" copy /Y "%%F" "dist\Logibot\%%F" >nul
)
if exist "logibot_icon.ico" copy /Y "logibot_icon.ico" "dist\Logibot\logibot_icon.ico" >nul
echo {"version":"2.1.0"} > "dist\Logibot\version_local.json"

echo.
echo ============================================
echo  LISTO! Logibot.exe generado correctamente
echo ============================================
echo.
echo  Ubicacion: dist\Logibot\Logibot.exe
echo.
echo  Proximos pasos:
echo  1. Proba el exe haciendo doble clic en dist\Logibot\Logibot.exe
echo  2. Si funciona, comprime la carpeta dist\Logibot en un .zip
echo  3. Distribuye el .zip
echo.

start explorer "%~dp0dist\Logibot"
pause