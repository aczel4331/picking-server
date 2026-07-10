"""
logibot_updater.py — Auto-updater de Logibot Picking Pro
=========================================================
Verifica si hay nueva version en GitHub Releases y la instala.

FLUJO DE INSTALACION EN WINDOWS:
  1. Se descarga el nuevo .zip en una carpeta temporal
  2. Se crea un script .bat que:
     a. Espera que el proceso actual cierre (usando su PID)
     b. Extrae el zip sobre la carpeta de instalacion
     c. Reinicia Logibot.exe
  3. El bat se ejecuta en background y la app actual se cierra

FIX SSL: algunas PCs con Windows desactualizado no tienen los certificados
raiz necesarios para verificar github.com. Se desactiva verificacion SSL
SOLO para descargas de GitHub (no afecta conexiones a Railway/ML).
"""

import os
import ssl
import sys
import json
import threading
import tempfile
import zipfile
import subprocess
import urllib.request
import tkinter as tk
from tkinter import ttk

# URL del version.json en el repo publico
VERSION_URL = "https://raw.githubusercontent.com/aczel4331/picking-server/main/version.json"

# Contexto SSL sin verificacion — solo para descargas de GitHub
_SSL_NO_VERIFY = ssl.create_default_context()
_SSL_NO_VERIFY.check_hostname = False
_SSL_NO_VERIFY.verify_mode    = ssl.CERT_NONE


def _get_json(url, timeout=10):
    """GET que devuelve dict JSON. Ignora errores de certificado SSL."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "LogibotUpdater/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_NO_VERIFY) as r:
        return json.loads(r.read().decode("utf-8"))


def verificar_actualizacion(version_actual: str, callback_disponible):
    """
    Verifica en background si hay nueva version.
    Si la hay, llama a callback_disponible(info_dict).
    """
    def _worker():
        try:
            data           = _get_json(VERSION_URL, timeout=12)
            version_remota = data.get("version", "0.0.0")
            if _es_mas_nueva(version_remota, version_actual):
                info = {
                    "version": version_remota,
                    "notas":   data.get("notas",   ""),
                    "url_zip": data.get("url_zip", ""),
                    "url_exe": data.get("url_exe", ""),
                }
                callback_disponible(info)
        except Exception as e:
            print(f"[UPDATER] No se pudo verificar actualizacion: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def _es_mas_nueva(remota: str, actual: str) -> bool:
    try:
        r = [int(x) for x in remota.strip().split(".")]
        a = [int(x) for x in actual.strip().split(".")]
        return r > a
    except Exception:
        return False


def _dir_instalacion() -> str:
    """Carpeta donde vive el Logibot.exe actual."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def descargar_e_instalar(url: str, version: str,
                         callback_progreso,
                         callback_listo,
                         callback_error):
    """
    Descarga el .zip del release en background y prepara la instalacion.
    La instalacion real ocurre en reiniciar_con_nueva_version().
    """
    def _worker():
        try:
            # Guardar zip en carpeta temporal del sistema
            tmp_dir  = tempfile.gettempdir()
            zip_path = os.path.join(tmp_dir, f"Logibot_update_v{version}.zip")

            req = urllib.request.Request(
                url, headers={"User-Agent": "LogibotUpdater/1.0"})

            with urllib.request.urlopen(req, context=_SSL_NO_VERIFY, timeout=120) as resp:
                total      = int(resp.headers.get("Content-Length", 0))
                descargado = 0
                bloque     = 8192

                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(bloque)
                        if not chunk:
                            break
                        f.write(chunk)
                        descargado += len(chunk)
                        if total > 0:
                            callback_progreso(int(descargado / total * 100))

            callback_progreso(100)

            # Guardar ruta del zip para que reiniciar_con_nueva_version lo use
            _guardar_ruta_descarga(zip_path)
            callback_listo()

        except Exception as e:
            callback_error(str(e))

    threading.Thread(target=_worker, daemon=True).start()


def _guardar_ruta_descarga(ruta: str):
    """Persiste la ruta del zip descargado."""
    path = os.path.join(_dir_instalacion(), "_update_pending.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ruta)


def reiniciar_con_nueva_version():
    """
    Instala la actualizacion y reinicia Logibot.

    PROBLEMA EN WINDOWS: no se puede sobrescribir Logibot.exe mientras esta
    corriendo. La solucion es un script .bat que:
      1. Espera que el proceso actual (PID conocido) termine
      2. Extrae el zip sobre la carpeta de instalacion
      3. Ejecuta el nuevo Logibot.exe
      4. Se autoeliimina

    El bat se lanza con START /B para correr en background,
    luego sys.exit() cierra la app actual y el bat toma el control.
    """
    install_dir = _dir_instalacion()
    pending     = os.path.join(install_dir, "_update_pending.txt")

    if not os.path.exists(pending):
        print("[UPDATER] No hay actualizacion pendiente")
        return

    with open(pending, "r", encoding="utf-8") as f:
        zip_path = f.read().strip()

    os.remove(pending)

    if not os.path.exists(zip_path):
        print(f"[UPDATER] ZIP no encontrado: {zip_path}")
        return

    pid      = os.getpid()
    exe_dest = os.path.join(install_dir, "Logibot.exe")
    bat_path = os.path.join(tempfile.gettempdir(), "logibot_install.bat")

    # Construir el batch de instalacion
    bat_content = f"""@echo off
title Instalando Logibot...
echo Esperando que Logibot cierre...

:: Esperar a que el proceso actual (PID {pid}) termine
:WAIT_LOOP
tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto WAIT_LOOP
)

echo Instalando actualizacion...

:: Extraer el ZIP sobre la carpeta de instalacion
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '{zip_path}' -DestinationPath '{install_dir}' -Force"

if errorlevel 1 (
    echo ERROR al extraer el ZIP.
    pause
    goto CLEANUP
)

echo Iniciando nueva version...
timeout /t 1 /nobreak >NUL

:: Buscar Logibot.exe (puede estar en subcarpeta dist\\Logibot\\)
if exist "{exe_dest}" (
    start "" "{exe_dest}"
) else (
    :: Buscar en subcarpetas
    for /r "{install_dir}" %%f in (Logibot.exe) do (
        start "" "%%f"
        goto CLEANUP
    )
    echo No se encontro Logibot.exe
    pause
)

:CLEANUP
:: Borrar el ZIP temporal y este bat
del /Q "{zip_path}" 2>NUL
del /Q "%~f0" 2>NUL
"""

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    print(f"[UPDATER] Lanzando instalador: {bat_path}")

    # Lanzar el bat en una ventana minimizada y separada del proceso actual
    subprocess.Popen(
        ["cmd.exe", "/C", "start", "/MIN", bat_path],
        shell=False,
        creationflags=subprocess.CREATE_NEW_CONSOLE
        if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0
    )

    # Cerrar la app actual — el bat tomara el control
    sys.exit(0)


# ── Widget: Banner de actualizacion disponible ────────────────────────────────

class BannerActualizacion(tk.Frame):
    """
    Banner naranja en la parte superior cuando hay nueva version disponible.
    """
    def __init__(self, parent, info: dict, on_instalar=None, **kwargs):
        super().__init__(parent, bg="#854D0E", **kwargs)
        self.place(relx=0, rely=0, relwidth=1, height=36)

        version = info.get("version", "?")
        notas   = info.get("notas",   "")

        tk.Label(
            self,
            text=f"🔄  Nueva version {version} disponible — {notas[:60]}",
            bg="#854D0E", fg="#FEF3C7",
            font=("Segoe UI", 9)
        ).pack(side="left", padx=12)

        if on_instalar:
            tk.Button(
                self,
                text="Actualizar ahora",
                bg="#F59E0B", fg="white", relief="flat",
                font=("Segoe UI Semibold", 9), cursor="hand2",
                padx=8, pady=2, bd=0,
                command=on_instalar
            ).pack(side="right", padx=8)

        tk.Button(
            self,
            text="x", bg="#854D0E", fg="#FEF3C7",
            relief="flat", font=("Segoe UI", 9),
            cursor="hand2", padx=6, pady=2, bd=0,
            command=self.destroy
        ).pack(side="right")