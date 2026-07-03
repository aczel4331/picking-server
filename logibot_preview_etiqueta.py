"""
logibot_preview_etiqueta.py
===========================
Vista previa de etiqueta ANTES de imprimir.

Descarga el PDF de una etiqueta ML, aplica la personalización
(logo + texto) y muestra el resultado en una ventana Tkinter
con zoom, sin necesidad de abrir el navegador.

Uso desde app_deposito.py:
    from logibot_preview_etiqueta import mostrar_preview_etiqueta
    mostrar_preview_etiqueta(parent, order_id, config, url_base, clave_api)
"""

import tkinter as tk
from tkinter import messagebox
import threading
import urllib.request
import json
import base64
import os
import sys
import io
import tempfile


def mostrar_preview_etiqueta(parent, order_id: str, config: dict,
                              url_base: str, clave_api: str):
    """
    Descarga y muestra la etiqueta personalizada en una ventana de previsualización.

    Parámetros:
        parent     : ventana padre Tkinter
        order_id   : ID de la orden ML
        config     : dict de config de la app (logo, texto, posición, etc.)
        url_base   : URL base del servidor Railway
        clave_api  : API key del servidor
    """
    win = _VentanaPreview(parent, order_id, config, url_base, clave_api)
    win.abrir()


class _VentanaPreview:
    """Ventana de previsualización de etiqueta."""

    def __init__(self, parent, order_id, config, url_base, clave_api):
        self._parent   = parent
        self._order_id = order_id
        self._config   = config
        self._url_base = url_base.rstrip("/")
        self._clave    = clave_api
        self._win      = None
        self._pdf_bytes = None
        self._zoom     = 1.0

    def abrir(self):
        self._win = tk.Toplevel(self._parent)
        self._win.title(f"👁 Vista previa — Pedido #{self._order_id}")
        self._win.geometry("620x760")
        self._win.configure(bg="#0F172A")
        self._win.resizable(True, True)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self._win, bg="#1E293B", height=50)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=f"👁  Vista previa — Pedido #{self._order_id}",
                 bg="#1E293B", fg="white",
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=16, pady=12)

        # Botones acción
        btn_frame = tk.Frame(hdr, bg="#1E293B")
        btn_frame.pack(side="right", padx=10)
        tk.Button(btn_frame, text="🔍+", bg="#334155", fg="white",
                  relief="flat", font=("Segoe UI", 10), padx=8, pady=4,
                  cursor="hand2", command=self._zoom_in).pack(side="left", padx=2)
        tk.Button(btn_frame, text="🔍-", bg="#334155", fg="white",
                  relief="flat", font=("Segoe UI", 10), padx=8, pady=4,
                  cursor="hand2", command=self._zoom_out).pack(side="left", padx=2)
        tk.Button(btn_frame, text="🖨 Imprimir",
                  bg="#10B981", fg="white",
                  relief="flat", font=("Segoe UI Semibold", 10), padx=12, pady=4,
                  cursor="hand2", command=self._imprimir).pack(side="left", padx=(8,2))

        # ── Canvas para mostrar la etiqueta ───────────────────────────────────
        canvas_frame = tk.Frame(self._win, bg="#1E293B")
        canvas_frame.pack(fill="both", expand=True, padx=0, pady=0)

        self._canvas = tk.Canvas(canvas_frame, bg="#1E293B",
                                 highlightthickness=0)
        vsb = tk.Scrollbar(canvas_frame, orient="vertical",
                           command=self._canvas.yview)
        hsb = tk.Scrollbar(canvas_frame, orient="horizontal",
                           command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vsb.set,
                               xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        # ── Estado: cargando ──────────────────────────────────────────────────
        self._lbl_estado = tk.Label(self._win,
            text="⬇  Descargando etiqueta...",
            bg="#0F172A", fg="#94A3B8",
            font=("Segoe UI", 10))
        self._lbl_estado.pack(pady=8)

        # ── Descargar en background ───────────────────────────────────────────
        threading.Thread(target=self._descargar, daemon=True).start()

    def _descargar(self):
        """Descarga la etiqueta personalizada del servidor."""
        try:
            url = f"{self._url_base}/api/etiqueta/{self._order_id}"

            # Construir config de personalización (igual que al imprimir)
            config_etiq = {
                "etiqueta_logo_pos":  self._config.get("etiqueta_logo_pos",  "superior_der"),
                "etiqueta_logo_size": self._config.get("etiqueta_logo_size", 20),
                "etiqueta_texto":     self._config.get("etiqueta_texto",     "").strip(),
                "etiqueta_texto_pos": self._config.get("etiqueta_texto_pos", "abajo"),
            }

            logo_path = self._config.get("etiqueta_logo_path", "").strip()
            if logo_path and os.path.exists(logo_path):
                with open(logo_path, "rb") as f:
                    config_etiq["etiqueta_logo_b64"] = base64.b64encode(
                        f.read()).decode("utf-8")
                config_etiq["etiqueta_logo_ext"] = os.path.splitext(logo_path)[1].lower()

            body = json.dumps(config_etiq).encode("utf-8")
            req  = urllib.request.Request(
                url, data=body,
                headers={"Accept": "application/pdf",
                         "X-API-Key": self._clave,
                         "Content-Type": "application/json"},
                method="POST")

            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()

            if raw[:4] != b"%PDF":
                raise ValueError(f"Respuesta no es PDF: {raw[:100]}")

            self._pdf_bytes = raw
            self._win.after(0, self._renderizar)

        except Exception as e:
            self._win.after(0, lambda err=str(e): self._mostrar_error(err))

    def _renderizar(self):
        """Convierte el PDF a imagen y la muestra en el canvas."""
        if not self._pdf_bytes:
            return
        try:
            import fitz   # PyMuPDF
            doc  = fitz.open(stream=self._pdf_bytes, filetype="pdf")
            page = doc[0]
            mat  = fitz.Matrix(self._zoom * 2.5, self._zoom * 2.5)  # ~150dpi
            pix  = page.get_pixmap(matrix=mat, alpha=False)

            from PIL import Image, ImageTk
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            self._photo = ImageTk.PhotoImage(img)

            self._canvas.delete("all")
            self._canvas.create_image(10, 10, anchor="nw", image=self._photo)
            self._canvas.configure(scrollregion=(0, 0,
                                                  pix.width + 20,
                                                  pix.height + 20))
            doc.close()

            self._lbl_estado.config(
                text="✅ Vista previa lista  —  Lo que ves es lo que se imprime",
                fg="#10B981")

        except ImportError:
            # Si no tiene PyMuPDF, guardar el PDF y abrirlo con el lector del sistema
            self._lbl_estado.config(
                text="⚠ PyMuPDF no disponible — abriendo con el lector del sistema",
                fg="#F59E0B")
            self._abrir_pdf_externo()
        except Exception as e:
            self._mostrar_error(f"Error renderizando: {e}")

    def _abrir_pdf_externo(self):
        """Guarda el PDF temporal y lo abre con el lector predeterminado."""
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               f"logibot_preview_{self._order_id}.pdf")
            with open(tmp, "wb") as f:
                f.write(self._pdf_bytes)
            import subprocess
            os.startfile(tmp)   # Windows
        except Exception as e:
            self._mostrar_error(f"No se pudo abrir el PDF: {e}")

    def _mostrar_error(self, msg: str):
        self._lbl_estado.config(text=f"❌ {msg[:120]}", fg="#EF4444")

    def _zoom_in(self):
        self._zoom = min(self._zoom + 0.25, 3.0)
        self._renderizar()

    def _zoom_out(self):
        self._zoom = max(self._zoom - 0.25, 0.5)
        self._renderizar()

    def _imprimir(self):
        """Cierra la preview y confirma impresión."""
        if messagebox.askyesno(
                "Confirmar impresión",
                f"¿Imprimir la etiqueta del pedido #{self._order_id}?",
                parent=self._win):
            self._win.destroy()
            # La app principal maneja la impresión real
            # Aquí solo emitimos un evento custom si fuera necesario
