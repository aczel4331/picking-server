"""Impresion: el diagnostico de etiqueta en blanco SOLO anota en el log; nunca bloquea."""
import os, sys, tempfile, types, zipfile
sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, REPO)
import app_deposito as A
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
logs.clear(); verificar(mk("ok.pdf", buena))
check("etiqueta con contenido: queda el % en el log, sin aviso", any("% oscuro" in m for m in logs) and not any("ATENCION" in m for m in logs), logs)
logs.clear()
try: verificar(mk("v.pdf", vacia)); r = "sigue"
except Exception as e: r = "BLOQUEO " + type(e).__name__
check("hoja en blanco: NO bloquea la impresion", r == "sigue", r)
check("hoja en blanco: queda ATENCION en el log (PDF vacio)", any("ATENCION" in m and "ya esta vacio" in m for m in logs), logs)
logs.clear()
try: verificar(mk("t.pdf", texto_blanco)); r = "sigue"
except Exception as e: r = "BLOQUEO " + type(e).__name__
check("PDF con texto y render blanco: NO bloquea", r == "sigue", r)
check("...y el log dice que es el render", any("ATENCION" in m and "render" in m for m in logs), logs)
z = os.path.join(os.environ.get("TEMP", ""), "z1.zip")
if os.path.exists(z):
    zf = zipfile.ZipFile(z); n = 0; avisos = 0
    for f in [x for x in zf.namelist() if x.endswith(".pdf")][:15]:
        p = os.path.join(d, "r.pdf"); open(p, "wb").write(zf.read(f)); n += 1
        logs.clear(); verificar(A.recortar_pdf_a_contenido(p)); avisos += any("ATENCION" in m for m in logs)
    check(f"{n} etiquetas reales de Everest: ningun aviso falso", avisos == 0, avisos)
print(f"\nRESULTADO impresion: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
