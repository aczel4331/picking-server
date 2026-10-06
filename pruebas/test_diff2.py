"""Diferencial: código de PRODUCCIÓN (HEAD) vs código NUEVO, mismos datos y mismos pedidos
reales, mismo tráfico de Everest. Las respuestas deben ser idénticas (salvo campos nuevos
y relojes)."""
import os, sys, json, time, shutil, subprocess, copy
import requests
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(S_DIR, "snap", "pedidos_live.json")
REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = {"viejo": (os.path.join(S_DIR, "old_head"), "5121"), "nuevo": (REPO_DEFAULT, "5122")}
USERS = [{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
         {"usuario": "sup2", "clave": "abcd", "nombre": "Sup", "cuenta_id": "cuenta_2", "rol": "supervisor"},
         {"usuario": "colector1", "clave": "c1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}]
RUIDO = {"ultima_act", "ts", "ultimo_refresh", "servidor_ts", "ultima_actualizacion", "inicio_ts", "ts_scan", "token", "tienda", "lote_id", "ultimos_scans", "clave_firma"}


def limpiar(o):
    if isinstance(o, dict):
        return {k: limpiar(v) for k, v in o.items() if k not in RUIDO}
    if isinstance(o, list):
        return [limpiar(v) for v in o]
    return o


def correr(nombre):
    repo, port = REPOS[nombre]
    data = os.path.join(S_DIR, f"data_diff2_{nombre}"); shutil.rmtree(data, ignore_errors=True); os.makedirs(data)
    json.dump(USERS, open(os.path.join(data, "usuarios.json"), "w"))
    env = dict(os.environ, PORT=port, DATA_DIR=data, PICKING_API_KEY="everest2024", REPO=repo, PEDIDOS_SNAP=SNAP,
               APP_SECRET_KEY="k" * 40, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
    p = subprocess.Popen([sys.executable, os.path.join(S_DIR, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S_DIR, f"diff2_{nombre}.log"), "w"), stderr=subprocess.STDOUT)
    B = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try: requests.get(B + "/api/ping", timeout=1); break
            except Exception: time.sleep(0.5)
        K = {"X-API-Key": "everest2024"}
        out = {}
        real = json.load(open(SNAP, encoding="utf-8"))["pedidos"]
        col = [x for x in real if x.get("logistica") == "cross_docking" and x.get("substatus") == "ready_to_print"][:12]
        skus = {}
        for x in col:
            for it in x["items"]:
                s_ = (it.get("sku") or it.get("item_id")).upper(); skus[s_] = skus.get(s_, 0) + it["cantidad"]
        pl = {"fase": 1, "grupos": [{"pasillo": "P1", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": "V", "item_id": ""} for k, v in skus.items()]}],
              "colecta": {}, "colecta_completa": False, "total_skus": len(skus), "total_uds": sum(skus.values()),
              "pedidos": {str(i + 1): {"_order_id": x["order_id"], "_shipping_id": x["shipping_id"], "_cuenta": "cuenta_2", "comprador": x["comprador"], "items": [{"sku": (it.get("sku") or it.get("item_id")).upper(), "cantidad": it["cantidad"]} for it in x["items"]]} for i, x in enumerate(col)},
              "canal": "colecta", "operario": "C1", "usuario": "colector1", "cuenta_id": "cuenta_2", "lote_id": "L1"}
        pasos = [("POST", "/api/auth/login", {"json": {"usuario": "colector1", "clave": "c1"}}),
                 ("POST", "/api/auth/login", {"json": {"usuario": "colector1", "clave": "mala"}}),
                 ("GET", "/api/pedidos", {}), ("GET", "/api/cuentas", {}), ("GET", "/auth/status", {}),
                 ("GET", "/api/pedidos/estados", {"headers": K}), ("GET", "/api/branding", {}),
                 ("POST", "/api/subir_estado", {"json": pl, "headers": K}),
                 ("GET", "/api/estado?usuario=colector1&cuenta_id=cuenta_2&canal=colecta", {}),
                 ("POST", "/api/escanear", {"json": {"sku": list(skus)[0], "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}}),
                 ("POST", "/api/escanear", {"json": {"sku": "NOEXISTE", "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}}),
                 ("GET", "/api/estado?usuario=colector1&cuenta_id=cuenta_2&canal=colecta", {}),
                 ("POST", "/api/reset_sku", {"json": {"sku": list(skus)[0], "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}}),
                 ("GET", "/api/estado?canal=colecta", {}),
                 ("GET", "/api/fase2/pedidos-completos?usuario=colector1&cuenta_id=cuenta_2&canal=colecta", {}),
                 ("GET", "/api/lote-en-vivo", {"headers": K}),
                 ("POST", "/api/config-app", {"json": {"codigo_supervisor": "1111"}, "headers": K}),
                 ("GET", "/api/config-app", {"headers": K}),
                 ("POST", "/api/alertas/sin-stock", {"json": {"operario": "c", "alertas": [{"sku": "A", "nombre": "a"}]}, "headers": K}),
                 ("GET", "/api/alertas/sin-stock", {"headers": K}),
                 ("POST", "/api/metricas/subir", {"json": {"metricas": [{"ts": "2026-10-05 10:00", "canal": "colecta", "operario": "c", "n_pedidos": 2}]}, "headers": K}),
                 ("GET", "/api/metricas", {"headers": K}), ("GET", "/api/auth/usuarios", {"headers": K}),
                 ("GET", f"/api/pedidos/impreso/{col[0]['order_id']}", {}),
                 ("POST", f"/api/pedidos/marcar_impreso/{col[0]['order_id']}", {}),
                 ("GET", f"/api/pedidos/impreso/{col[0]['order_id']}", {}),
                 ("POST", "/api/limpiar", {"json": {"usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}, "headers": K}),
                 ("GET", "/api/estado?usuario=colector1&cuenta_id=cuenta_2&canal=colecta", {})]
        for i, (m, ruta, kw) in enumerate(pasos):
            r = requests.request(m, B + ruta, timeout=30, **kw)
            try: cuerpo = limpiar(r.json())
            except Exception: cuerpo = r.text[:200]
            out[f"{i:02d} {m} {ruta[:60]}"] = (r.status_code, cuerpo)
        return out
    finally:
        p.kill()


a, b = correr("viejo"), correr("nuevo")
dif = 0
for k in a:
    if a[k] != b[k]:
        dif += 1
        print("DIFERENCIA en", k)
        va, vb = a[k][1], b[k][1]
        def planos(o, p=""):
            if isinstance(o, dict):
                for kk, vv in o.items(): yield from planos(vv, p + "/" + kk)
            elif isinstance(o, list):
                for i, vv in enumerate(o): yield from planos(vv, p + f"[{i}]")
            else: yield p, o
        da, db = dict(planos(va)), dict(planos(vb))
        for kk in sorted(set(da) | set(db)):
            if da.get(kk) != db.get(kk): print("   ", kk, "viejo=", da.get(kk), "nuevo=", db.get(kk))
print(f"\nDIFERENCIAL: {len(a)} llamadas de Everest, {dif} diferencias")
sys.exit(1 if dif else 0)
