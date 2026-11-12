"""Seguridad de la app de escritorio 4.1.36 (auditoría del 11/11/2026): un login rechazado por el servidor
no entra como "administrador offline", la verificación HTTPS queda encendida, los nombres de archivo armados
con datos del servidor no salen de su carpeta y la página del celular escapa lo que muestra. Todo local."""
import os, sys, re, json, ssl, threading, http.server
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
sys.path.insert(0, REPO)
import app_deposito as A

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)[:200]))
    if c: ok += 1
    else: fa += 1

print("1) login: rechazo del servidor != sin conexión")
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        cod, msg = RESP["cod"], RESP["msg"]
        self.send_response(cod); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"ok": cod == 200, "msg": msg}).encode())
    def log_message(self, *a): pass
RESP = {"cod": 401, "msg": "Usuario o clave incorrectos"}
srv = http.server.HTTPServer(("127.0.0.1", 5140), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

class Login:
    _auth = A.VentanaLogin._auth
    def __init__(self, url):
        self.config = {"servidor_nube": url, "codigo_supervisor": "1234"}
        self.res = []
    def after(self, ms, fn): fn()
    def _ok(self, u): self.res.append(("ok", u))
    def _error(self, m): self.res.append(("error", m))

for cod in (401, 403, 429):
    RESP.update(cod=cod, msg=f"rechazo {cod}")
    l = Login("http://127.0.0.1:5140"); l._auth("admin", "1234")
    check(f"servidor responde {cod} a admin + código de supervisor -> NO entra, muestra el motivo",
          l.res == [("error", f"rechazo {cod}")], l.res)
l = Login("http://127.0.0.1:1"); l._auth("admin", "1234")
check("sin conexión de verdad, admin + código -> entra offline (como antes)", l.res and l.res[0][0] == "ok", l.res)
l = Login("http://127.0.0.1:1"); l._auth("admin", "9999")
check("sin conexión y código equivocado -> no entra", l.res and l.res[0][0] == "error", l.res)
srv.shutdown()

print("2) HTTPS verificado")
src = open(os.path.join(REPO, "app_deposito.py"), encoding="utf-8").read()
check("app_deposito.py ya no apaga la verificación en ningún lado", "CERT_NONE" not in src and "check_hostname = False" not in src)
for f in ("logibot_updater.py", "logibot_dashboard.py"):
    check(f"{f} tampoco", "CERT_NONE" not in open(os.path.join(REPO, f), encoding="utf-8").read())
ctx = A._ssl_ca(ssl.create_default_context())
check("el contexto exige certificado y nombre del servidor", ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)
check("trae los certificados raíz de certifi (PCs con Windows viejo)", ctx.cert_store_stats().get("x509_ca", 0) > 50, ctx.cert_store_stats())

print("3) nombres de archivo con datos del servidor")
for malo in ("..\\..\\Windows\\x", "../../etc/x", "12 34", "C:\\x", ""):
    n = A._nombre_seguro(malo)
    check(f"{malo!r} -> {n!r} sin separadores ni '..'", re.fullmatch(r"[0-9A-Za-z_-]{1,40}", n) is not None, n)
check("un order_id normal no cambia", A._nombre_seguro("2000019162353774") == "2000019162353774")
j, png = A._marca_paths("..\\..\\x")
check("caché de marca de tienda queda en la carpeta de datos", os.path.dirname(j) == A._dir_datos_usuario()
      and os.path.dirname(png) == A._dir_datos_usuario(), (j, png))
check("la de Everest conserva su nombre de siempre", A._marca_paths("") == (A.MARCA_CACHE_PATH, A.LOGO_CACHE_PATH))

print("4) página del celular")
m = open(os.path.join(REPO, "templates", "movil.html"), encoding="utf-8").read()
check("tiene esc() y lo usa en pasillo, SKU, nombre y estantería",
      "function esc(" in m and all(x in m for x in ("${esc(p)}", "${esc(it.sku)}", "${esc(it.nombre", "${esc(it.estanteria)}")))
check("el SKU ya no va dentro de un onclick", "abrirImagen('${it.sku}')" not in m and "abrirImagen(this.dataset.sku)" in m)
check("la imagen se pide con el SKU codificado", "encodeURIComponent(sku)" in m)
import subprocess, shutil
if shutil.which("node"):
    js = re.search(r"function esc\(v\) \{.*?\n\}", m, re.S).group(0)
    out = subprocess.run(["node", "-e", js + "\nprocess.stdout.write(esc(`<img src=x onerror=alert(1)>\"'&`))"],
                         capture_output=True, text=True).stdout
    check("esc() neutraliza etiquetas y comillas", "<" not in out and '"' not in out and "'" not in out and "&amp;" in out, out)

print(f"\nRESULTADO seguridad app: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
