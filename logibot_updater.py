"""
logibot_updater.py
==================
Auto-actualizador de Logibot Picking Pro.

Flujo:
1. Al arrancar la app, este módulo consulta GitHub Releases (o un JSON público)
   buscando una versión más nueva.
2. Si existe → muestra un banner no-bloqueante en la UI con un botón
   "Descargar actualización".
3. El usuario hace clic → se descarga el nuevo .exe en la MISMA carpeta
   con nombre temporal "Logibot_nueva.exe".
4. Se abre un pequeño script .bat que:
   a) Espera a que el proceso actual cierre.
   b) Renombra/reemplaza el .exe viejo con el nuevo.
   c) Lanza la nueva versión automáticamente.
5. La app actual se cierra sola.

No requiere desinstalar ni borrar nada. Los datos (config.json, excel_cache.json)
se conservan porque están en la misma carpeta y no son parte del .exe.

Configuración (en GitHub):
──────────────────────────
Crear un archivo público en tu repo:
  https://raw.githubusercontent.com/TU_USUARIO/TU_REPO/main/version.json

Contenido de version.json:
{
  "version": "2.2.0",
  "url_exe": "https://github.com/TU_USUARIO/TU_REPO/releases/download/v2.2.0/Logibot.exe",
  "notas": "Nuevas funciones: dashboard de métricas, vista previa de etiquetas"
}
"""

import threading
import urllib.request
import json
import os
import sys
import subprocess
import tempfile

# ── URL del archivo de versión (editar con tu repo de GitHub) ─────────────────
VERSION_URL = (
    "https://raw.githubusercontent.com/EVEREST-GROUP-UY/logibot/main/version.json"
)

# Tiempo de espera entre verificaciones (segundos)
CHECK_INTERVAL = 3600   # cada 1 hora


def _comparar_versiones(v_actual: str, v_nueva: str) -> bool:
    """Devuelve True si v_nueva es mayor que v_actual."""
    try:
        def _partes(v):
            return [int(x) for x in v.strip().lstrip("v").split(".")]
        return _partes(v_nueva) > _partes(v_actual)
    except Exception:
        return False


def verificar_actualizacion(version_actual: str, callback_hay_update):
    """
    Verifica en background si hay una versión nueva disponible.
    Si la hay, llama a callback_hay_update(info_dict) en el thread de fondo.
    El callback debe usar root.after(0, ...) para actualizar la UI.

    Parámetros:
        version_actual       : str  ej. "2.1.0"
        callback_hay_update  : callable(info: dict)
            info = {
                "version": "2.2.0",
                "url_exe": "https://...",
                "notas":   "Cambios en esta versión"
            }
    """
    def _worker():
        try:
            req = urllib.request.Request(
                VERSION_URL,
                headers={"User-Agent": "Logibot-Updater/1.0",
                         "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read().decode("utf-8"))

            v_nueva = info.get("version", "0.0.0")
            if _comparar_versiones(version_actual, v_nueva):
                print(f"[UPDATER] Nueva versión disponible: {v_nueva} "
                      f"(actual: {version_actual})")
                callback_hay_update(info)
            else:
                print(f"[UPDATER] App al día (v{version_actual})")
        except Exception as e:
            print(f"[UPDATER] No se pudo verificar actualización: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def descargar_e_instalar(url_exe: str, version_nueva: str,
                         callback_progreso=None,
                         callback_listo=None,
                         callback_error=None):
    """
    Descarga el nuevo .exe y prepara el reemplazo automático.

    Parámetros:
        url_exe           : URL del nuevo .exe
        version_nueva     : str  ej. "2.2.0"
        callback_progreso : callable(porcentaje: int)  0-100
        callback_listo    : callable()  cuando está listo para reiniciar
        callback_error    : callable(msg: str)
    """
    def _worker():
        try:
            # Directorio donde está el .exe actual (o el .py en dev)
            if getattr(sys, "frozen", False):
                # Corriendo como .exe generado por PyInstaller
                carpeta = os.path.dirname(sys.executable)
                exe_actual = sys.executable
            else:
                # Corriendo como .py en desarrollo
                carpeta = os.path.dirname(os.path.abspath(__file__))
                exe_actual = os.path.join(carpeta, "Logibot.exe")

            exe_nuevo   = os.path.join(carpeta, "_Logibot_nueva.exe")
            bat_archivo = os.path.join(carpeta, "_actualizar.bat")

            print(f"[UPDATER] Descargando {url_exe}")
            print(f"[UPDATER] Destino temporal: {exe_nuevo}")

            # ── Descargar con progreso ────────────────────────────────────────
            req = urllib.request.Request(
                url_exe, headers={"User-Agent": "Logibot-Updater/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                descargado = 0
                chunk = 65536   # 64 KB
                with open(exe_nuevo, "wb") as f:
                    while True:
                        bloque = resp.read(chunk)
                        if not bloque:
                            break
                        f.write(bloque)
                        descargado += len(bloque)
                        if total and callback_progreso:
                            pct = int(descargado * 100 / total)
                            callback_progreso(pct)

            print(f"[UPDATER] Descarga completa: {descargado} bytes")

            # ── Crear .bat que reemplaza el exe y lanza la nueva versión ──────
            # El .bat espera a que este proceso termine, reemplaza y relanza.
            bat_contenido = f"""@echo off
title Actualizando Logibot...
echo Actualizando Logibot Picking Pro a v{version_nueva}...
echo Por favor espere...

:: Esperar a que el proceso viejo termine (máx 30 segundos)
:WAIT
tasklist /FI "IMAGENAME eq {os.path.basename(exe_actual)}" 2>NUL | find /I /N "{os.path.basename(exe_actual)}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >NUL
    goto WAIT
)

:: Reemplazar el exe viejo con el nuevo
echo Instalando nueva version...
del /F /Q "{exe_actual}" 2>NUL
move /Y "{exe_nuevo}" "{exe_actual}"

:: Lanzar la nueva version
echo Lanzando Logibot v{version_nueva}...
start "" "{exe_actual}"

:: Autolimpieza
del /F /Q "%~f0"
"""
            with open(bat_archivo, "w", encoding="cp1252") as f:
                f.write(bat_contenido)

            print(f"[UPDATER] .bat creado: {bat_archivo}")

            if callback_listo:
                callback_listo()

        except Exception as e:
            print(f"[UPDATER] Error descargando: {e}")
            if callback_error:
                callback_error(str(e))

    threading.Thread(target=_worker, daemon=True).start()


def reiniciar_con_nueva_version():
    """
    Lanza el .bat de actualización y cierra la app actual.
    Llamar solo cuando la descarga ya completó.
    """
    try:
        if getattr(sys, "frozen", False):
            carpeta = os.path.dirname(sys.executable)
        else:
            carpeta = os.path.dirname(os.path.abspath(__file__))

        bat = os.path.join(carpeta, "_actualizar.bat")
        if os.path.exists(bat):
            subprocess.Popen(
                ["cmd", "/c", bat],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                close_fds=True
            )
            print("[UPDATER] Proceso de actualización iniciado. Cerrando app...")
            # Dar tiempo al bat para arrancar antes de cerrar
            import time; time.sleep(1)
            sys.exit(0)
        else:
            print(f"[UPDATER] .bat no encontrado: {bat}")
    except Exception as e:
        print(f"[UPDATER] Error al reiniciar: {e}")


# ── Widget de notificación de actualización (banner en la UI) ─────────────────

class BannerActualizacion:
    """
    Banner discreto que aparece en la parte superior de la ventana cuando
    hay una actualización disponible. No bloquea la app.

    Uso:
        banner = BannerActualizacion(root, info_update, on_instalar_callback)
        banner.mostrar()
    """

    def __init__(self, parent_frame, info: dict, on_instalar):
        """
        parent_frame : tk.Frame donde se inserta el banner
        info         : dict con version, url_exe, notas
        on_instalar  : callable() → se llama cuando el usuario confirma instalar
        """
        self._parent = parent_frame
        self._info   = info
        self._on_ins = on_instalar
        self._frame  = None

    def mostrar(self):
        if self._frame:
            return
        import tkinter as tk

        v = self._info.get("version", "?")
        notas = self._info.get("notas", "")

        # Banner amarillo discreto
        self._frame = tk.Frame(self._parent, bg="#854D0E",
                               relief="flat", bd=0)
        self._frame.pack(fill="x", side="top", before=self._parent.winfo_children()[0]
                         if self._parent.winfo_children() else None)

        tk.Label(self._frame,
                 text=f"🔄  Nueva versión v{v} disponible",
                 bg="#854D0E", fg="#FEF3C7",
                 font=("Segoe UI Semibold", 9)).pack(side="left", padx=12, pady=6)

        if notas:
            tk.Label(self._frame,
                     text=f"— {notas[:80]}",
                     bg="#854D0E", fg="#FDE68A",
                     font=("Segoe UI", 8)).pack(side="left", padx=(0,8))

        tk.Button(self._frame,
                  text="⬇  Descargar e instalar",
                  bg="#F59E0B", fg="white",
                  activebackground="#D97706", activeforeground="white",
                  font=("Segoe UI Semibold", 9),
                  relief="flat", cursor="hand2",
                  padx=10, pady=4, bd=0,
                  command=self._confirmar).pack(side="right", padx=8, pady=4)

        tk.Button(self._frame,
                  text="✕", bg="#854D0E", fg="#FEF3C7",
                  activebackground="#713F12", activeforeground="white",
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  padx=6, pady=4, bd=0,
                  command=self.ocultar).pack(side="right", padx=4)

    def ocultar(self):
        if self._frame:
            self._frame.destroy()
            self._frame = None

    def _confirmar(self):
        import tkinter.messagebox as mb
        v = self._info.get("version", "?")
        notas = self._info.get("notas", "")
        msg = (f"¿Actualizar Logibot a la versión {v}?\n\n"
               f"Qué hay de nuevo:\n{notas}\n\n"
               f"La app se cerrará y se reabrirá automáticamente.\n"
               f"Tus datos (config, Excel, lotes) no se borran.")
        if mb.askyesno("Actualizar Logibot", msg):
            self._on_ins()
