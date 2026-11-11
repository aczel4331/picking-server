"""Seguridad, paso 1 (auditoría del 11/11/2026): rutas que entregaban credenciales de Mercado Libre,
HTML de afuera mostrado sin escapar en los paneles, etiqueta de otro envío guardada bajo un pedido,
logout por enlace y cuerpos gigantes. Todo local (servidor en memoria con datos inventados)."""
import os, sys, json, tempfile, base64, io
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, PICKING_API_KEY="everest2024", APP_SECRET_KEY="s" * 40, ML_APP_ID="x",
                  ML_SECRET_KEY="x", ML_API_URL="http://127.0.0.1:1")
os.environ.pop("PLATAFORMA_KEY", None)
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S
from datetime import datetime, timedelta

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)[:200]))
    if c: ok += 1
    else: fa += 1

XSS = '<script>alert(1)</script>'
K = {"X-API-Key": "everest2024"}
C = S.app.test_client()
S._cuentas["cuenta_2"] = {"access_token": "AT-FALSO-NO-SALIR", "refresh_token": "RT-FALSO-NO-SALIR",
                          "expires_at": datetime.now() + timedelta(hours=5), "user_id": "1", "nickname": "TIENDA"}
S._pedidos_ml["2000000000000001"] = {"order_id": "2000000000000001", "shipping_id": "44000000001", "_cuenta": "cuenta_2",
                                     "comprador": "B", "items": [], "impreso": False, "logistica": "self_service"}

def panel(rol="admin"):
    with C.session_transaction() as ses:
        ses["admin_panel_usuario"] = "admin"; ses["admin_panel_rol"] = rol; ses["admin_panel_cuenta_id"] = "todas"

print("1) credenciales de Mercado Libre")
r = C.get("/api/test_credentials")
check("/api/test_credentials ya no existe", r.status_code == 404, r.status_code)
r = C.get("/api/tokens_export", headers=K)
check("tokens_export con la clave de los exe -> 403 y sin tokens", r.status_code == 403 and b"AT-FALSO" not in r.data
      and b"RT-FALSO" not in r.data, (r.status_code, r.data[:120]))
os.environ["PLATAFORMA_KEY"] = "p" * 32
r = C.get("/api/tokens_export", headers=dict(K, **{"X-Plataforma-Clave": "otra-clave-equivocada-123"}))
check("tokens_export con clave de plataforma equivocada -> 403", r.status_code == 403, r.status_code)
r = C.get("/api/tokens_export", headers=dict(K, **{"X-Plataforma-Clave": "p" * 32}))
check("tokens_export con PLATAFORMA_KEY -> funciona", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
os.environ["PLATAFORMA_KEY"] = "corta"
r = C.get("/api/tokens_export", headers=dict(K, **{"X-Plataforma-Clave": "corta"}))
check("una PLATAFORMA_KEY corta no habilita nada", r.status_code == 403, r.status_code)
os.environ.pop("PLATAFORMA_KEY", None)

print("2) etiquetas: número de venta y envío")
for ruta in ("/api/etiqueta/%3Cb%3Ex", "/api/etiqueta/a%22b/guardada", "/api/etiqueta/a.b/descargar"):
    r = C.get(ruta)
    check(f"order_id con símbolos rechazado: {ruta}", r.status_code == 400 and b"<b>" not in r.data, (r.status_code, r.data[:80]))
r = C.post("/api/etiqueta/2000000000000001", json={"shipping_id": "44999999999"}, headers=K)
check("POST con el shipping_id de OTRO envío -> 409, no se guarda nada", r.status_code == 409
      and not os.path.exists(os.path.join(S.ETIQUETAS_DIR, "2000000000000001.pdf")), (r.status_code, r.data[:120]))
r = C.post("/api/etiqueta/2000000000000001", json={"shipping_id": "44000000001<x>"}, headers=K)
check("shipping_id que no es numérico -> 409", r.status_code == 409, r.status_code)
r = C.post("/api/etiqueta/2000000000000001", json={"shipping_id": "44000000001"}, headers=K)
check("el shipping_id correcto sigue el camino de siempre (ML falso: sin etiqueta, pero no 409)", r.status_code != 409, r.status_code)
S._pedidos_ml["2000000000000002"] = {"order_id": "2000000000000002", "shipping_id": "", "_cuenta": "cuenta_2",
                                     "comprador": XSS, "items": [{"sku": XSS}], "impreso": False}
r = C.get("/api/etiqueta/2000000000000002")
check("pedido sin envío: comprador y SKU escapados en el error", XSS.encode() not in r.data and b"&lt;script&gt;" in r.data, r.data[-300:])

print("3) paneles: lo que viene de afuera se muestra escapado")
os.makedirs(S.ETIQUETAS_DIR, exist_ok=True)
with open(os.path.join(S.ETIQUETAS_DIR, "2000000000000009.json"), "w", encoding="utf-8") as f:
    json.dump({"order_id": "2000000000000009", "shipping_id": XSS, "comprador": XSS, "items": XSS, "canal": "flex",
               "ts": "2026-11-11 10:00:00", "fecha": "2026-11-11", "_cuenta": "cuenta_2"}, f)
panel()
r = C.get("/etiquetas")
check("/etiquetas abre", r.status_code == 200, r.status_code)
check("/etiquetas: comprador, envío y productos escapados", XSS.encode() not in r.data, r.data.count(b"<script>alert"))
r = C.post("/api/metricas/subir", headers=K, json={"metricas": [{"ts": "2026-11-11 10:00:00", "canal": "flex",
           "operario": XSS, "n_pedidos": 1, "total_uds": 1, "duracion_seg": 60,
           "order_ids": [{"order_id": XSS, "pack_id": XSS, "shipping_id": XSS}], "skus": {XSS: 1}}]})
check("métrica con HTML aceptada (se guarda tal cual)", r.status_code == 200, r.data[:100])
r = C.get("/estadisticas")
check("/estadisticas abre", r.status_code == 200, r.status_code)
check("/estadisticas: operario, ventas y SKU escapados", XSS.encode() not in r.data, r.data.count(b"<script>alert"))
from openpyxl import Workbook
wb = Workbook(); wb.active.append(["SKU", "NOMBRE"]); bio = io.BytesIO(); wb.save(bio)
r = C.post("/api/config-app/excel", headers=K, json={"excel_b64": base64.b64encode(bio.getvalue()).decode(),
                                                     "filename": XSS + ".xlsx"})
check("Excel con nombre raro aceptado", r.status_code == 200, r.data[:100])
r = C.get("/config")
check("/config: nombre del Excel escapado", r.status_code == 200 and XSS.encode() not in r.data
      and b"&lt;script&gt;alert(1)&lt;/script&gt;.xlsx" in r.data, r.status_code)
r = C.get("/auth/callback?error=" + "%3Cimg%20src%3Dx%3E")
check("/auth/callback con error armado: no inyecta HTML", b"<img src=x>" not in r.data, r.data[-200:])

print("4) logout y tamaño")
r = C.get("/auth/logout?cuenta=cuenta_2")
check("logout por enlace (GET) ya no desconecta", r.status_code == 405 and "cuenta_2" in S._cuentas, r.status_code)
with C.session_transaction() as ses: ses.clear()
r = C.post("/auth/logout?cuenta=cuenta_2")
check("logout POST sin clave ni sesión -> 401", r.status_code == 401 and "cuenta_2" in S._cuentas, r.status_code)
r = C.post("/auth/logout?cuenta=cuenta_9", headers=K)
check("logout POST con clave -> ok (JSON)", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
grande = b"{" + b" " * (2 * 1024 * 1024) + b"}"
r = C.post("/api/escanear", data=grande, content_type="application/json")
check("escanear con 2 MB -> 413", r.status_code == 413, r.status_code)
r = C.post("/api/escanear", json={"sku": "X", "usuario": "u", "cuenta_id": "cuenta_2", "canal": "flex"})
check("escanear normal sigue respondiendo", r.status_code != 413, r.status_code)
r = C.post("/api/config-app/excel", headers=K, data=b"x" * (65 * 1024 * 1024), content_type="application/json")
check("cuerpo de 65 MB en ruta común -> 413", r.status_code == 413, r.status_code)
r = C.post("/api/admin/app/publicar", data=b"x" * (65 * 1024 * 1024), content_type="application/octet-stream")
check("publicar admite más de 64 MB (no corta por tamaño)", r.status_code != 413, r.status_code)

print(f"\nRESULTADO seguridad paso 1: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
