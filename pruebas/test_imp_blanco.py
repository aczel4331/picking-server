"""Impresion: no se gasta una etiqueta cuando el PDF/render esta en blanco."""
import os, sys, io, tempfile, types, zipfile
sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, REPO)
import app_deposito as A, pymupdf
from PIL import Image
from reportlab.pdfgen import canvas
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1
cls = next(c for c in vars(A).values() if isinstance(c, type) and hasattr(c, "_verificar_no_blanca"))
logs = []
yo = types.SimpleNamespace(_log_impresion=lambda m: logs.append(m))
def verificar(ruta):
    img = cls._renderizar_pagina_imagen(yo, ruta)
    return cls._verificar_no_blanca(yo, img, ruta, "T")
d = tempfile.mkdtemp()
def mk(nombre, f):
    p = os.path.join(d, nombre); c = canvas.Canvas(p, pagesize=(283, 538)); f(c); c.save(); return p
def buena(c):
    c.setFont("Helvetica-Bold", 14)
    for i in range(10): c.drawString(20, 500 - i * 40, f"ETIQUETA LINEA {i} CODIGO 12345")
    c.rect(20, 20, 240, 80, fill=1)
def vacia(c): c.showPage()
def texto_blanco(c):
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica", 12)
    for i in range(10): c.drawString(20, 500 - i * 30, f"TEXTO EN BLANCO LINEA NUMERO {i}")
check("etiqueta con contenido: pasa", verificar(mk("ok.pdf", buena)) is None)
check("queda el % en el log", any("% oscuro" in m for m in logs), logs)
try: verificar(mk("v.pdf", vacia)); r = "paso"
except A.EtiquetaVaciaError: r = "vacia"
except Exception as e: r = type(e).__name__
check("hoja en blanco: NO se imprime (EtiquetaVaciaError)", r == "vacia", r)
try: verificar(mk("t.pdf", texto_blanco)); r = "paso"
except A.EtiquetaVaciaError: r = "vacia"
except RuntimeError: r = "render"
check("PDF con texto pero render blanco: prueba otro metodo (RuntimeError)", r == "render", r)
T = os.environ.get("TEMP", "")
z = os.path.join(T, "z1.zip")
if os.path.exists(z):
    zf = zipfile.ZipFile(z); n = 0; mal = 0
    for f in [x for x in zf.namelist() if x.endswith(".pdf")][:15]:
        p = os.path.join(d, "r.pdf"); open(p, "wb").write(zf.read(f)); n += 1
        try: verificar(A.recortar_pdf_a_contenido(p))
        except Exception: mal += 1
    check(f"{n} etiquetas reales de Everest: ninguna se bloquea", mal == 0, mal)
print(f"\nRESULTADO impresion: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
