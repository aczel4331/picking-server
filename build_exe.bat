@echo off
title Compilando Logibot Picking Pro...
color 0A
echo.
echo  ============================================
echo   Compilador de Logibot Picking Pro v2.1.0
echo   GitHub: github.com/aczel4331/picking-server
echo  ============================================
echo.

cd /d "%~dp0"

:: ── Verificar Python ─────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado. Instala Python desde python.org
    pause & exit /b 1
)

:: ── Instalar dependencias si faltan ─────────────────────────────────────────
echo [0/5] Verificando dependencias...
pip install pyinstaller --quiet
pip install Pillow --quiet
pip install PyMuPDF --quiet
pip install openpyxl --quiet
pip install reportlab --quiet
pip install pypdf --quiet

:: ── Generar icono si no existe ───────────────────────────────────────────────
if not exist "logibot_icon.ico" (
    echo [0/5] Generando icono Logibot...
    python -c "
from PIL import Image, ImageDraw
import struct, io

img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

def rr(draw, xy, r, fill):
    x1,y1,x2,y2 = xy
    draw.rectangle([x1+r,y1,x2-r,y2], fill=fill)
    draw.rectangle([x1,y1+r,x2,y2-r], fill=fill)
    for cx,cy in [(x1,y1),(x2-2*r,y1),(x1,y2-2*r),(x2-2*r,y2-2*r)]:
        draw.ellipse([cx,cy,cx+2*r,cy+2*r], fill=fill)

rr(draw,[8,8,248,248],40,(30,58,138,255))
draw.rectangle([70,60,110,190], fill=(255,255,255,255))
draw.rectangle([70,160,180,200], fill=(255,255,255,255))
draw.ellipse([150,60,200,110], fill=(251,191,36,255))

sizes=[16,32,48,64,128,256]
imgs=[]
for s in sizes:
    buf=io.BytesIO()
    img.resize((s,s),Image.Resampling.LANCZOS).save(buf,format='PNG')
    imgs.append((s,buf.getvalue()))

with open('logibot_icon.ico','wb') as f:
    f.write(struct.pack('<HHH',0,1,len(imgs)))
    off=6+len(imgs)*16
    for s,d in imgs:
        w=h=0 if s==256 else s
        f.write(struct.pack('<BBBBHHII',w,h,0,0,1,32,len(d),off))
        off+=len(d)
    for s,d in imgs:
        f.write(d)
print('Icono generado')
"
)

:: ── Limpiar build anterior ───────────────────────────────────────────────────
echo [1/5] Limpiando compilaciones anteriores...
if exist "dist\Logibot" rmdir /S /Q "dist\Logibot" 2>nul
if exist "build" rmdir /S /Q "build" 2>nul
if exist "Logibot.spec" del /Q "Logibot.spec" 2>nul

:: ── Compilar ─────────────────────────────────────────────────────────────────
echo [2/5] Compilando Logibot.exe (puede tardar 2-5 minutos)...
echo.

pyinstaller ^
    --name "Logibot" ^
    --onedir ^
    --windowed ^
    --icon "logibot_icon.ico" ^
    --add-data "templates/movil.html;templates" ^
    --add-data "logibot_updater.py;." ^
    --add-data "logibot_dashboard.py;." ^
    --add-data "logibot_preview_etiqueta.py;." ^
    --hidden-import "PIL._tkinter_finder" ^
    --hidden-import "PIL.Image" ^
    --hidden-import "PIL.ImageTk" ^
    --hidden-import "fitz" ^
    --hidden-import "reportlab.pdfgen.canvas" ^
    --hidden-import "reportlab.lib.colors" ^
    --hidden-import "reportlab.lib.utils" ^
    --hidden-import "pypdf" ^
    --hidden-import "openpyxl" ^
    --hidden-import "tkinter" ^
    --hidden-import "tkinter.ttk" ^
    --hidden-import "tkinter.filedialog" ^
    --hidden-import "tkinter.messagebox" ^
    --collect-all "reportlab" ^
    --collect-all "fitz" ^
    --collect-all "PIL" ^
    --noconfirm ^
    app_deposito.py

if errorlevel 1 (
    echo.
    echo [ERROR] Compilacion fallida. Lee los mensajes de arriba.
    echo.
    echo Causas comunes:
    echo   - Faltan librerias: corre "pip install -r requirements_desktop.txt"
    echo   - El archivo app_deposito.py tiene errores de sintaxis
    pause & exit /b 1
)

:: ── Copiar archivos de datos ─────────────────────────────────────────────────
echo [3/5] Copiando archivos de datos...
set DIST=dist\Logibot

:: Conservar datos existentes del usuario (no sobreescribir)
for %%F in (config.json excel_cache.json metrics.json) do (
    if exist "%%F" (
        if not exist "%DIST%\%%F" (
            copy /Y "%%F" "%DIST%\%%F" >nul
            echo   %%F copiado
        ) else (
            echo   %%F conservado ^(no sobreescrito^)
        )
    )
)

if exist "logo_cache.png" copy /Y "logo_cache.png" "%DIST%\logo_cache.png" >nul
if exist "logibot_icon.ico" copy /Y "logibot_icon.ico" "%DIST%\logibot_icon.ico" >nul

:: ── Crear archivo de version local ──────────────────────────────────────────
echo [4/5] Creando version_local.json...
echo {"version":"2.1.0","build":"%DATE% %TIME%"} > "%DIST%\version_local.json"

:: ── Crear requirements para referencia ──────────────────────────────────────
echo [5/5] Finalizando...

echo.
echo  ==========================================
echo   BUILD EXITOSO
echo  ==========================================
echo.
echo   Ejecutable: %DIST%\Logibot.exe
echo.
echo   Para distribuir:
echo   1. Comprimi toda la carpeta dist\Logibot\ en un .zip
echo   2. Subi el .zip a GitHub Releases como "Logibot.zip"
echo   3. Actualizá version.json en el repo con la nueva version
echo.
echo   Los datos del usuario se conservan entre versiones:
echo   - config.json     (configuracion de impresora, etc)
echo   - excel_cache.json (base de pasillos)
echo   - metrics.json    (historial de metricas)
echo.

explorer "%~dp0dist\Logibot"
pause
