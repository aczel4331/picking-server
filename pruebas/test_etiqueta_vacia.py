"""Etiquetas: el servidor no acepta ni guarda PDFs vacios de ML, y la personalizacion no
puede dejar la etiqueta sin contenido. Usa un 'Mercado Libre' falso local."""
import os, sys, json, time, shutil, subprocess, io, threading, base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import requests
from PIL import Image
from reportlab.pdfgen import canvas
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT, MLP = "5134", 5141
B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_etq")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

def pdf_bueno():
    b = io.BytesIO(); c = canvas.Canvas(b, pagesize=(283, 538))
    c.setFont("Helvetica", 10)
    for i in range(12): c.drawString(20, 500 - i * 20, f"Venta ID 20000188 envio linea {i} Remitente EVERESTSHOP")
    im = Image.new("RGB", (120, 40), (0, 0, 0)); t = io.BytesIO(); im.save(t, "PNG"); t.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(t), 20, 200, 120, 40); c.save(); return b.getvalue()

def pdf_vacio():
    b = io.BytesIO(); c = canvas.Canvas(b, pagesize=(283, 538)); c.showPage(); c.save(); return b.getvalue()

MODO = {"v": "pdf_vacio_luego_bueno"}
LLAMADAS = []
class ML(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/shipment_labels":
            rt = parse_qs(u.query).get("response_type", [""])[0]
            LLAMADAS.append(rt)
            if MODO["v"] == "todo_vacio" or (MODO["v"] == "pdf_vacio_luego_bueno" and rt == "pdf"):
                data = pdf_vacio()
            else:
                data = pdf_bueno()
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()
mlsrv = HTTPServer(("127.0.0.1", MLP), ML); threading.Thread(target=mlsrv.serve_forever, daemon=True).start()

shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "colector1", "clave": "c1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}], open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"),
           APP_SECRET_KEY="s" * 40, ML_API_URL=f"http://127.0.0.1:{MLP}", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S, "etq.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)
    K = {"X-API-Key": "everest2024"}
    peds = [p for p in requests.get(B + "/api/pedidos").json()["pedidos"] if p.get("shipping_id")][:4]
    def etiqueta(p, cfg=None):
        return requests.post(B + f"/api/etiqueta/{p['order_id']}", headers=K, json=cfg or {}, timeout=30)
    def guardada(p):
        return os.path.exists(os.path.join(DATA, "etiquetas", p["order_id"] + ".pdf"))
    sys.path.insert(0, REPO)
    import importlib.util
    # contenido con pypdf (misma funcion del servidor)
    src = open(os.path.join(REPO, "server.py"), encoding="utf-8").read()
    ns = {}
    i0 = src.index("def _pdf_contenido"); i1 = src.index("def _aplicar_personalizacion_etiqueta")
    exec(src[i0:i1].replace("\r\n", "\n"), ns)
    check("detector: etiqueta buena tiene contenido", ns["_pdf_con_contenido"](pdf_bueno()))
    check("detector: hoja en blanco se reconoce", not ns["_pdf_con_contenido"](pdf_vacio()))
    check("detector: texto ilegible no bloquea (None = ok)", ns["_pdf_con_contenido"](b"%PDF-basura"))

    print("ML devuelve vacio en 'pdf' y bueno en 'pdf2'")
    MODO["v"] = "pdf_vacio_luego_bueno"; LLAMADAS.clear()
    r = etiqueta(peds[0])
    check("responde PDF", r.status_code == 200 and r.content[:4] == b"%PDF", r.status_code)
    check("entrega la version con contenido", ns["_pdf_con_contenido"](r.content) and ns["_pdf_contenido"](r.content)[0] >= 100, ns["_pdf_contenido"](r.content))
    check("probo el siguiente tipo", LLAMADAS[:2] == ["pdf", "pdf2"], LLAMADAS)
    check("lo guardado en cache es el bueno", guardada(peds[0]) and ns["_pdf_contenido"](open(os.path.join(DATA, "etiquetas", peds[0]["order_id"] + ".pdf"), "rb").read())[0] >= 100)

    print("ML devuelve vacio siempre")
    MODO["v"] = "todo_vacio"; LLAMADAS.clear()
    r = etiqueta(peds[1])
    check("igual responde (no rompe)", r.status_code == 200)
    check("NO se guarda en cache el vacio", not guardada(peds[1]))
    MODO["v"] = "bueno"
    r = etiqueta(peds[1])
    check("el pedido siguiente pide de nuevo a ML y guarda el bueno", guardada(peds[1]) and ns["_pdf_contenido"](r.content)[0] >= 100)

    print("flujo normal con logo")
    logo = io.BytesIO(); Image.new("RGBA", (200, 60), (200, 0, 0, 255)).save(logo, "PNG")
    cfg = {"etiqueta_logo_b64": base64.b64encode(logo.getvalue()).decode(), "etiqueta_logo_pos": "superior_izq", "etiqueta_logo_size": 15, "etiqueta_texto": "Hola"}
    r = etiqueta(peds[2], cfg)
    c = ns["_pdf_contenido"](r.content)
    check("con logo y texto: sigue con el contenido y el logo", r.status_code == 200 and c[0] >= 100 and c[1] >= 2, c)
    r2 = requests.get(B + f"/api/etiqueta/{peds[2]['order_id']}/guardada", headers=K)
    check("/guardada devuelve lo mismo", r2.status_code == 200 and r2.content == r.content)

    print("auditoria")
    r = requests.get(B + "/api/admin/etiquetas-auditoria", headers=K).json()
    check("sin vacias en lo guardado", r["ok"] and r["vacias"] == [] and r["total"] >= 3, r)
    open(os.path.join(DATA, "etiquetas", "9999999999.pdf"), "wb").write(pdf_vacio())
    r = requests.get(B + "/api/admin/etiquetas-auditoria", headers=K).json()
    check("detecta una guardada en blanco", r["vacias"] == ["9999999999"], r)
    check("exige clave maestra", requests.get(B + "/api/admin/etiquetas-auditoria").status_code == 401)
finally:
    srv.kill(); mlsrv.shutdown()
print(f"\nRESULTADO etiquetas: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
