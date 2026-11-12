"""Paneles web (auditoría 11/11/2026, paso 5): ninguna página trae la clave del servidor en su código; trae un
token firmado del usuario que inició sesión, y con ese token funcionan las llamadas que hace cada panel.
Todo local (servidor en memoria con datos inventados)."""
import os, sys, re, json, tempfile
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, PICKING_API_KEY="everest2024", APP_SECRET_KEY="s" * 40, ML_APP_ID="x",
                  ML_SECRET_KEY="x", ML_API_URL="http://127.0.0.1:1")
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)[:200]))
    if c: ok += 1
    else: fa += 1

S._usuarios.extend([{"usuario": "sup_e", "clave": "c-sup", "nombre": "Supervisor E", "cuenta_id": "cuenta_2", "rol": "supervisor"},
                    {"usuario": "jefe", "clave": "c-jefe", "nombre": "Jefe", "cuenta_id": "todas", "rol": "admin"}])
CLAVES = ("everest2024", "everest2025", S.API_KEY)

for usuario, clave in (("sup_e", "c-sup"), ("jefe", "c-jefe")):
    print(f"── panel como {usuario}")
    C = S.app.test_client()
    r = C.post("/admin/usuarios", data={"usuario": usuario, "clave": clave})
    check("entra al panel", r.status_code in (200, 302), r.status_code)
    for ruta in ("/admin/usuarios", "/config", "/estadisticas", "/etiquetas"):
        html = C.get(ruta).get_data(as_text=True)
        tiene = [k for k in CLAVES if k and (f"'{k}'" in html or f'"{k}"' in html)]
        toks = set(re.findall(r"tk1\.[A-Za-z0-9_.\-]+", html))
        check(f"{ruta}: sin la clave del servidor, con un token firmado", not tiene and toks, (tiene, len(toks)))
        for t in list(toks)[:1]:
            d, err = S._leer_token(t)
            check(f"{ruta}: el token es de {usuario}", not err and d.get("u") == usuario, (d, err))
    tok = next(iter(set(re.findall(r"tk1\.[A-Za-z0-9_.\-]+", C.get("/config").get_data(as_text=True)))), "")
    H = {"X-API-Key": tok}
    for m, ruta, body in (("GET", "/api/config-app", None), ("GET", "/api/auth/usuarios", None), ("GET", "/api/alertas/sin-stock", None),
                          ("GET", "/api/auth/pendientes", None), ("GET", "/api/lote-en-vivo", None),
                          ("POST", "/api/config-app", {"codigo_supervisor": "4321"})):
        r = getattr(S.app.test_client(), m.lower())(ruta, headers=H, json=body)
        check(f"con ese token: {m} {ruta} responde", r.status_code == 200, (r.status_code, r.data[:100]))

print("── sin sesión")
C = S.app.test_client()
for ruta in ("/config", "/estadisticas", "/etiquetas"):
    r = C.get(ruta)
    check(f"{ruta} sin sesión: no muestra panel ni credenciales", r.status_code in (302, 401, 403) or ("tk1." not in r.get_data(as_text=True)
          and "everest2024" not in r.get_data(as_text=True)), r.status_code)

print(f"\nRESULTADO paneles: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
