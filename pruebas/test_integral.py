"""PRUEBA INTEGRAL de Logibot con los pedidos REALES de Everest (foto de solo lectura de
producción) sobre un servidor LOCAL: nada se escribe en producción ni en Mercado Libre.

Secciones: A login/tokens · B tráfico de Everest (clientes viejos) · C tienda nueva con
token · D ataques y aislamiento · E pantallas de la app (sin 'Railway')."""
import os, sys, json, time, shutil, subprocess, base64, io, re
import requests
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("REPO", REPO_DEFAULT)
PORT = os.environ.get("TPORT", "5111")
B = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(S_DIR, "data_int")
SNAP = os.path.join(S_DIR, "snap", "pedidos_live.json")
SECRET = "secreto-de-prueba-1234567890-abcdefghijklmnop"
KEY = "everest2024"
ok_n = fail_n = 0
fallas = []


def check(n, c, extra=""):
    global ok_n, fail_n
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f"   → {extra}"))
    if c: ok_n += 1
    else: fail_n += 1; fallas.append(n)


def png(color=(10, 120, 200, 255)):
    b = io.BytesIO(); Image.new("RGBA", (120, 40), color).save(b, "PNG"); return b.getvalue()


def hk(k=KEY, **extra):
    d = {"X-API-Key": k} if k else {}
    d.update(extra); return d


def ht(tok, **extra):
    d = {"X-API-Key": tok}; d.update(extra); return d


def req(m, path, headers=None, **kw):
    return requests.request(m, B + path, headers=headers or {}, timeout=30, **kw)


def j(r):
    try: return r.json()
    except Exception: return {}


shutil.rmtree(DATA, ignore_errors=True); os.makedirs(os.path.join(DATA, "etiquetas"))
real = json.load(open(SNAP, encoding="utf-8"))["pedidos"]
OID_E = str(real[0]["order_id"])          # un pedido real de Everest
USERS = [{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
         {"usuario": "sup2", "clave": "abcd", "nombre": "Sup Everest", "cuenta_id": "cuenta_2", "rol": "supervisor"},
         {"usuario": "colector1", "clave": "c1", "nombre": "Colector 1", "cuenta_id": "cuenta_2", "rol": "operario"},
         {"usuario": "colector2", "clave": "c2", "nombre": "Colector 2", "cuenta_id": "cuenta_2", "rol": "operario"}]
json.dump(USERS, open(os.path.join(DATA, "usuarios.json"), "w"))
with open(os.path.join(DATA, "etiquetas", OID_E + ".pdf"), "wb") as f: f.write(b"%PDF-1.4 fake-everest")
json.dump({"order_id": OID_E, "canal": "colecta", "comprador": "x", "ts": "2026-10-05 10:00:00", "_cuenta": "cuenta_2"},
          open(os.path.join(DATA, "etiquetas", OID_E + ".json"), "w"))

env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY=KEY, REPO=REPO, PEDIDOS_SNAP=SNAP,
           APP_SECRET_KEY=SECRET, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x",
           PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S_DIR, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S_DIR, "int.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(0.5)

    # ═══ A. LOGIN Y TOKENS ═══════════════════════════════════════════════════
    print("A) login, tokens y marca")
    r = j(req("POST", "/api/auth/login", json={"usuario": "colector1", "clave": "c1"}))
    check("Everest: login ok con token", r.get("ok") and r.get("token", "").startswith("tk1."), str(r)[:200])
    check("Everest: respuesta histórica intacta (usuario/rol/cuenta)", set(r["usuario"]) == {"usuario", "nombre", "cuenta_id", "rol"} and r["usuario"]["cuenta_id"] == "cuenta_2")
    check("Everest: marca primaria (tienda_id vacío)", r["tienda"]["tienda_id"] == "")
    TK_E = r["token"]
    r = j(req("POST", "/api/auth/login", json={"usuario": "admin", "clave": "1234"}))
    check("admin general: login ok", r.get("ok") and r["usuario"]["rol"] == "admin")
    check("clave mala → 401", req("POST", "/api/auth/login", json={"usuario": "colector1", "clave": "no"}).status_code == 401)
    check("sin datos → 400", req("POST", "/api/auth/login", json={}).status_code == 400)
    cods = [req("POST", "/api/auth/login", json={"usuario": "ratelimit", "clave": "x"}).status_code for _ in range(10)]
    check("límite de intentos por usuario (429)", 429 in cods, str(cods))

    # tienda nueva (la crea el admin general con credenciales) + sus usuarios por el panel
    ADM = {"X-Admin-Usuario": "admin", "X-Admin-Clave": "1234"}
    r = req("POST", "/api/admin/tiendas", headers=ADM, json={"nombre": "Tienda Beta", "subtitulo": "Sub B", "logo_b64": base64.b64encode(png()).decode()})
    TID = j(r)["tienda"]["id"]
    check("tienda nueva creada (con logo)", r.status_code == 200 and TID.startswith("t_"), r.text[:200])
    ses = requests.Session()
    rp = ses.post(B + "/admin/usuarios", data={"usuario": "admin", "clave": "1234"})
    check("panel: admin general entra (cookie)", rp.status_code == 200 and "Gestion de Usuarios" in rp.text)
    check("panel Everest: sigue incrustando la clave de siempre", KEY in rp.text)
    for u in [{"usuario": "badmin", "clave": "bpw1", "nombre": "Admin Beta", "cuenta_id": TID, "rol": "admin"},
              {"usuario": "bop", "clave": "bpw2", "nombre": "Op Beta", "cuenta_id": TID, "rol": "operario"}]:
        rr = ses.post(B + "/api/auth/usuarios", json=u, headers=hk())
        check(f"admin general crea usuario de la tienda ({u['usuario']})", rr.status_code == 200 and j(rr).get("ok"), rr.text[:150])
    r = j(req("POST", "/api/auth/login", json={"usuario": "bop", "clave": "bpw2"}))
    check("tienda nueva: login con SU marca", r.get("ok") and r["tienda"]["tienda_id"] == TID and r["tienda"]["nombre"] == "Tienda Beta" and r["tienda"]["tiene_logo"], str(r)[:200])
    TK_B = r["token"]
    TK_BA = j(req("POST", "/api/auth/login", json={"usuario": "badmin", "clave": "bpw1"}))["token"]

    # pedidos y etiqueta de la tienda nueva (marcadores únicos)
    pedidos_b = [{"order_id": f"9000000000{i}", "pack_id": "", "shipping_id": f"7000{i}", "comprador": "MARCADOR_B",
                  "_cuenta": TID, "_nickname": "BETA_SHOP", "logistica": "cross_docking", "substatus": "ready_to_print",
                  "estado_envio": "ready_to_ship", "impreso": False, "tipo": "colecta", "fecha": "2026-10-05", "fecha_cierre": "2026-10-05",
                  "items": [{"sku": f"SKU-B-{i}", "item_id": f"MLUB{i}", "titulo": "Producto B", "cantidad": 1}]} for i in range(1, 4)]
    r = j(req("POST", "/__test/inject", json={"pedidos": pedidos_b, "cuentas": {TID: {"access_token": "AT-B", "refresh_token": "RT-B", "user_id": "222", "nickname": "BETA_SHOP"}}}))
    check("pedidos de la tienda nueva sembrados", r.get("ok"), str(r))
    OID_B = pedidos_b[0]["order_id"]
    with open(os.path.join(DATA, "etiquetas", OID_B + ".pdf"), "wb") as f: f.write(b"%PDF-1.4 fake-beta")
    json.dump({"order_id": OID_B, "canal": "colecta", "comprador": "MARCADOR_B", "ts": "2026-10-05 10:00:00", "_cuenta": TID},
              open(os.path.join(DATA, "etiquetas", OID_B + ".json"), "w"))
    MARC = [TID, "MARCADOR_B", "SKU-B-", "BETA_SHOP", OID_B]

    def sin_marcadores_b(texto):
        return not any(m in texto for m in MARC)

    # ═══ B. TRÁFICO DE EVEREST (clientes viejos: clave heredada / sin clave) ═══
    print("B) Everest con los clientes actuales (clave heredada y sin clave)")
    r = req("GET", "/api/pedidos")
    d = j(r)
    check("GET /api/pedidos (sin clave) → todos los pedidos reales", d.get("ok") and d["total"] == len(real), f"{d.get('total')}")
    check("…ninguno de otra tienda", sin_marcadores_b(r.text))
    check("…los pedidos reales llegan completos", {p["order_id"] for p in d["pedidos"]} == {str(p["order_id"]) for p in real})
    for ruta in ["/api/cuentas", "/auth/status", "/api/ping", "/api/token_status", "/api/etiquetas_cache", "/api/lote-en-vivo",
                 "/api/pedidos/estados", "/api/cortes", "/api/tokens-estado", "/api/diagnostico", "/api/verificar-etiquetas",
                 "/api/tokens_export", "/api/auth/usuarios", "/api/alertas/sin-stock", "/api/metricas", "/api/config-app"]:
        r = req("GET", ruta, headers=hk())
        check(f"GET {ruta} responde y no filtra datos de otra tienda", r.status_code in (200, 400) and sin_marcadores_b(r.text), f"{r.status_code} {r.text[:120]}")
    check("/auth/status: todos los pedidos y solo Everest", j(req("GET", "/auth/status"))["pedidos"] == len(real))
    cu = j(req("GET", "/api/cuentas"))["cuentas"]
    check("/api/cuentas: solo EVEREST_SHOPPING.UY", [c["nickname"] for c in cu] == ["EVEREST_SHOPPING.UY"], str(cu))

    # lote completo de Everest: subir → escanear → estado → deshacer → fase 2 → limpiar
    colecta = [p for p in real if (p.get("logistica") == "cross_docking" and p.get("substatus") == "ready_to_print")]
    skus = {}
    for p in colecta[:10]:
        for it in p["items"]:
            s_ = (it.get("sku") or it.get("item_id") or "").upper()
            skus[s_] = skus.get(s_, 0) + it["cantidad"]
    grupos = [{"pasillo": "P1 PRUEBA", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": "V1", "item_id": ""} for k, v in skus.items()]}]
    pl = {"fase": 1, "grupos": grupos, "colecta": {}, "colecta_completa": False, "total_skus": len(skus), "total_uds": sum(skus.values()),
          "pedidos": {str(i + 1): {"_order_id": p["order_id"], "_shipping_id": p.get("shipping_id", ""), "_cuenta": "cuenta_2", "comprador": p["comprador"],
                                    "items": [{"sku": (it.get("sku") or it.get("item_id")).upper(), "cantidad": it["cantidad"]} for it in p["items"]]}
                      for i, p in enumerate(colecta[:10])},
          "canal": "colecta", "operario": "Colector 1", "usuario": "colector1", "cuenta_id": "cuenta_2", "lote_id": "LOTE-E-1"}
    r = req("POST", "/api/subir_estado", headers=hk(), json=pl)
    check("subir lote real (clave heredada)", j(r).get("ok") and j(r)["clave"] == "cuenta_2:colector1", r.text[:150])
    q = "usuario=colector1&cuenta_id=cuenta_2&canal=colecta"
    e = j(req("GET", f"/api/estado?{q}"))
    check("celular: /api/estado devuelve el lote", e["cargado"] and e["total_skus"] == len(skus))
    check("celular: no ve lotes de otra tienda", sin_marcadores_b(json.dumps(e)))
    sku1 = next(iter(skus))
    r = j(req("POST", "/api/escanear", json={"sku": sku1, "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}))
    check("escaneo desde el celular", r.get("ok") and r["collected"] == 1, str(r))
    r = j(req("POST", "/api/escanear", json={"sku": "NO-EXISTE", "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}))
    check("SKU inexistente → no_encontrado", r.get("tipo") == "no_encontrado")
    r = j(req("POST", "/api/reset_sku", json={"sku": sku1, "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}))
    check("deshacer escaneo", r.get("ok"))
    j(req("POST", "/api/escanear", json={"sku": sku1, "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}))
    r = j(req("GET", "/api/lote-en-vivo", headers=hk()))
    check("panel supervisor: lote en vivo de Everest", r.get("ok") and any(l["canal"] == "cuenta_2:colector1" for l in r.get("lotes", [])), str(r)[:200])
    r = j(req("GET", f"/api/fase2/pedidos-completos?{q}"))
    check("fase 2: pedidos completos responde", r.get("ok"))
    oid0 = str(colecta[0]["order_id"])
    r = j(req("POST", f"/api/fase2/marcar-impresa/{oid0}", json={"usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}))
    check("fase 2: marcar impresa", r.get("ok"))
    check("pedido marcado como impreso", j(req("GET", f"/api/pedidos/impreso/{oid0}"))["impreso"] is True)
    r = j(req("POST", f"/api/pedidos/marcar_impreso/{oid0}"))
    check("marcar_impreso", r.get("ok"))
    check("etiqueta guardada de Everest se sirve", req("GET", f"/api/etiqueta/{OID_E}/guardada").status_code == 200)
    check("etiqueta de Everest por /api/etiqueta (caché)", req("GET", f"/api/etiqueta/{OID_E}").status_code == 200)
    z = req("GET", "/api/etiquetas/descargar-zip?canal=todos", headers=hk())
    check("ZIP de etiquetas: solo las de Everest", z.status_code == 200 and b"fake-beta" not in z.content)
    r = j(req("POST", "/api/pedidos/verificar-ahora", headers=hk(), json={"order_ids": [OID_B]}))
    check("verificar-ahora: un pedido ajeno 'no existe'", r["resultados"][OID_B]["existe"] is False, str(r))
    # sesiones y backup
    r = j(req("POST", "/api/sesion/guardar", headers=hk(), json={"usuario": "colector1", "canal": "colecta", "fase": 1, "pedidos": {"1": {"skus_requeridos": {"A": 1}}}}))
    check("sesión: guardar", r.get("ok"))
    check("sesión: recuperar", j(req("GET", "/api/sesion/recuperar/colector1", headers=hk()))["hay_sesion"] is True)
    check("sesión: borrar", j(req("POST", "/api/sesion/borrar/colector1", headers=hk())).get("ok"))
    check("lote-backup: subir y leer", j(req("POST", "/api/lote-backup", headers=hk(), json={"canal": "colecta", "pedidos": {"1": {}}})).get("ok")
          and j(req("GET", "/api/lote-backup/ultimo", headers=hk()))["backup"] is not None)
    # configuración, Excel, alertas, métricas, aprobaciones
    check("config-app: guardar/leer código supervisor", j(req("POST", "/api/config-app", headers=hk(), json={"codigo_supervisor": "EV-CODIGO-77"})).get("ok")
          and j(req("GET", "/api/config-app", headers=hk()))["config"]["codigo_supervisor"] == "EV-CODIGO-77")
    xl = base64.b64encode(b"PK-fake-excel-everest").decode()
    check("Excel de pasillos: subir y bajar", j(req("POST", "/api/config-app/excel", headers=hk(), json={"excel_b64": xl, "filename": "p.xlsx"})).get("ok")
          and j(req("GET", "/api/config-app/excel", headers=hk()))["excel_b64"] == xl)
    check("alertas sin stock: crear/leer/marcar/limpiar",
          j(req("POST", "/api/alertas/sin-stock", headers=hk(), json={"operario": "c1", "alertas": [{"sku": "X1", "nombre": "x"}]})).get("ok")
          and j(req("GET", "/api/alertas/sin-stock", headers=hk()))["no_leidas"] == 1
          and j(req("POST", "/api/alertas/sin-stock/leer", headers=hk())).get("ok")
          and j(req("POST", "/api/alertas/sin-stock/limpiar", headers=hk())).get("ok"))
    check("métricas: subir/leer", j(req("POST", "/api/metricas/subir", headers=hk(), json={"metricas": [{"ts": "2026-10-05 10:00", "canal": "colecta", "operario": "c1", "n_pedidos": 3, "cuenta_id": "cuenta_2"}]})).get("ok")
          and j(req("GET", "/api/metricas", headers=hk()))["total"] == 1)
    tk = j(req("POST", "/api/auth/solicitar", headers=hk(), json={"operario": "c1", "incompletos": []}))["token"]
    check("aprobación remota: solicitar → pendiente → aprobar", j(req("GET", f"/api/auth/estado/{tk}", headers=hk()))["aprobado"] is False
          and any(p["token"] == tk for p in j(req("GET", "/api/auth/pendientes", headers=hk()))["pendientes"])
          and j(req("POST", f"/api/auth/aprobar/{tk}", headers=hk())).get("ok")
          and j(req("GET", f"/api/auth/estado/{tk}", headers=hk()))["aprobado"] is True)
    # imagen sku (ML caído: debe responder sin romper)
    r = req("GET", f"/api/imagen-sku/{sku1}?item_id=MLU1")
    check("imagen-sku responde sin romper", r.status_code == 200, r.text[:100])
    # paneles web con sesión
    for ruta, texto in [("/admin/usuarios", "Usuarios"), ("/estadisticas", "stad"), ("/config", "onfig"), ("/etiquetas", "tiquetas")]:
        r = ses.get(B + ruta)
        check(f"panel {ruta} carga con sesión", r.status_code == 200 and texto in r.text and (ruta == "/admin/usuarios" or (ruta == "/estadisticas" and not any(m in r.text for m in MARC[1:])) or (ruta != "/estadisticas" and sin_marcadores_b(r.text))), f"{r.status_code}")
    check("panel sin sesión redirige al login", requests.get(B + "/estadisticas", allow_redirects=False).status_code == 302)
    # gestión de usuarios (admin general por sesión)
    r = ses.post(B + "/api/auth/usuarios", json={"usuario": "nuevo9", "clave": "zz9999", "nombre": "N", "cuenta_id": "cuenta_2", "rol": "operario"}, headers=hk())
    check("usuarios: crear operario de Everest", j(r).get("ok"))
    check("usuarios: cambiar clave", j(ses.put(B + "/api/auth/usuarios/nuevo9/clave", json={"clave": "nueva-clave"}, headers=hk())).get("ok")
          and j(req("POST", "/api/auth/login", json={"usuario": "nuevo9", "clave": "nueva-clave"})).get("ok"))
    check("usuarios: eliminar", j(ses.delete(B + "/api/auth/usuarios/nuevo9", headers=hk())).get("ok"))
    ses_sup = requests.Session(); ses_sup.post(B + "/admin/usuarios", data={"usuario": "sup2", "clave": "abcd"})
    r = ses_sup.post(B + "/api/auth/usuarios", json={"usuario": "op77", "clave": "zz9999", "nombre": "N", "cuenta_id": "cuenta_2", "rol": "operario"}, headers=hk())
    check("supervisor de Everest crea operarios (como siempre)", j(r).get("ok"), r.text[:120])
    r = ses_sup.post(B + "/api/auth/usuarios", json={"usuario": "adm77", "clave": "zz9999", "nombre": "N", "cuenta_id": "cuenta_2", "rol": "admin"}, headers=hk())
    check("…pero no puede crear admins", r.status_code == 403)
    ses_sup.delete(B + "/api/auth/usuarios/op77", headers=hk())

    # ═══ C. TIENDA NUEVA CON TOKEN ═══════════════════════════════════════════
    print("C) tienda nueva con su token")
    r = req("GET", "/api/pedidos", headers=ht(TK_B)); d = j(r)
    check("pedidos: solo los suyos (3)", d.get("ok") and d["total"] == 3 and all(p["_cuenta"] == TID for p in d["pedidos"]), f"{d.get('total')}")
    check("pedidos: nada de Everest", str(real[0]["order_id"]) not in r.text and "EVEREST" not in r.text)
    cu = j(req("GET", "/api/cuentas", headers=ht(TK_B)))["cuentas"]
    check("cuentas: solo la suya", [c["cuenta_id"] for c in cu] == [TID])
    check("auth/status: 3 pedidos", j(req("GET", "/auth/status", headers=ht(TK_B)))["pedidos"] == 3)
    plb = {"fase": 1, "grupos": [{"pasillo": "B1", "items": [{"sku": "SKU-B-1", "nombre": "B1", "req": 2, "pasillo": "B1", "estanteria": "", "item_id": ""}]}],
           "colecta": {}, "colecta_completa": False, "total_skus": 1, "total_uds": 2, "pedidos": {}, "canal": "colecta", "operario": "Op Beta",
           "usuario": "bop", "cuenta_id": "cuenta_2", "lote_id": "LOTE-B-1"}   # intenta declarar cuenta_2: el servidor lo ignora
    r = j(req("POST", "/api/subir_estado", headers=ht(TK_B), json=plb))
    check("tienda nueva: sube lote (clave la arma el servidor)", r.get("ok") and r["clave"] == f"{TID}:bop", str(r))
    check("…aunque declare cuenta_2 no pisa nada de Everest", j(req("GET", f"/api/estado?{q}"))["total_skus"] == len(skus))
    e = j(req("GET", "/api/estado?canal=colecta", headers=ht(TK_B)))
    check("tienda nueva: su celular ve su lote", e["cargado"] and e["total_skus"] == 1)
    check("tienda nueva: no ve lotes de Everest", all(k.startswith(TID) for k in e["canales_disponibles"]), str(list(e["canales_disponibles"])))
    r = j(req("POST", "/api/escanear", headers=ht(TK_B), json={"sku": "SKU-B-1", "canal": "colecta"}))
    check("tienda nueva: escanea su SKU", r.get("ok") and r["collected"] == 1, str(r))
    lv = j(req("GET", "/api/lote-en-vivo", headers=ht(TK_B)))
    check("lote-en-vivo: solo el suyo", [l["canal"] for l in lv["lotes"]] == [f"{TID}:bop"], str(lv))
    check("alertas: separadas por tienda",
          j(req("POST", "/api/alertas/sin-stock", headers=ht(TK_B), json={"operario": "bop", "alertas": [{"sku": "SKU-B-1", "nombre": "B"}]})).get("ok")
          and j(req("GET", "/api/alertas/sin-stock", headers=ht(TK_B)))["no_leidas"] == 1
          and j(req("GET", "/api/alertas/sin-stock", headers=hk()))["no_leidas"] == 0)
    check("métricas: separadas por tienda",
          j(req("POST", "/api/metricas/subir", headers=ht(TK_B), json={"metricas": [{"ts": "2026-10-05 11:00", "canal": "colecta", "operario": "bop", "n_pedidos": 1}]})).get("ok")
          and j(req("GET", "/api/metricas", headers=ht(TK_B)))["total"] == 1 and j(req("GET", "/api/metricas", headers=hk()))["total"] == 1)
    check("config-app: código supervisor propio, el de Everest intacto",
          j(req("POST", "/api/config-app", headers=ht(TK_B), json={"codigo_supervisor": "BETA-1", "tienda_nombre": "HACK"})).get("ok")
          and j(req("GET", "/api/config-app", headers=ht(TK_B)))["config"]["codigo_supervisor"] == "BETA-1"
          and j(req("GET", "/api/config-app", headers=hk()))["config"]["codigo_supervisor"] == "EV-CODIGO-77"
          and j(req("GET", "/api/branding"))["nombre"] == "")
    check("Excel: cada tienda el suyo",
          j(req("POST", "/api/config-app/excel", headers=ht(TK_B), json={"excel_b64": base64.b64encode(b"EXCEL-BETA").decode(), "filename": "b.xlsx"})).get("ok")
          and base64.b64decode(j(req("GET", "/api/config-app/excel", headers=ht(TK_B)))["excel_b64"]) == b"EXCEL-BETA"
          and j(req("GET", "/api/config-app/excel", headers=hk()))["excel_b64"] == xl)
    check("sesiones y backups: propios",
          j(req("POST", "/api/sesion/guardar", headers=ht(TK_B), json={"usuario": "bop", "canal": "colecta", "fase": 1, "pedidos": {"1": {}}})).get("ok")
          and j(req("POST", "/api/lote-backup", headers=ht(TK_B), json={"canal": "colecta", "pedidos": {"1": {"x": "MARCADOR_B"}}})).get("ok")
          and "MARCADOR_B" not in req("GET", "/api/lote-backup/ultimo", headers=hk()).text
          and "MARCADOR_B" in req("GET", "/api/lote-backup/ultimo", headers=ht(TK_B)).text)
    check("etiqueta propia se sirve", req("GET", f"/api/etiqueta/{OID_B}/guardada", headers=ht(TK_B)).status_code == 200)
    zb = req("GET", "/api/etiquetas/descargar-zip?canal=todos", headers=ht(TK_B))
    check("ZIP: solo etiquetas de la tienda", zb.status_code == 200 and b"fake-beta" in zb.content and b"fake-everest" not in zb.content)
    check("aprobación remota separada por tienda",
          (lambda t: j(req("GET", "/api/auth/pendientes", headers=ht(TK_B)))["pendientes"] and
           all(p["token"] == t for p in j(req("GET", "/api/auth/pendientes", headers=ht(TK_B)))["pendientes"]) and
           j(req("GET", f"/api/auth/estado/{t}", headers=hk())).get("ok") is False)(j(req("POST", "/api/auth/solicitar", headers=ht(TK_B), json={"operario": "bop"}))["token"]))
    # usuarios por token de admin de tienda
    lst = j(req("GET", "/api/auth/usuarios", headers=ht(TK_BA)))["usuarios"]
    check("admin de tienda: ve solo usuarios de su tienda", {u["usuario"] for u in lst} == {"badmin", "bop"}, str(lst))
    r = req("POST", "/api/auth/usuarios", headers=ht(TK_BA), json={"usuario": "bsup", "clave": "zz1234", "nombre": "B", "cuenta_id": TID, "rol": "supervisor"})
    check("admin de tienda: crea supervisor propio", j(r).get("ok"), r.text[:150])
    for etiqueta, cuerpo in [("un admin", {"usuario": "evil1", "clave": "zz1234", "cuenta_id": TID, "rol": "admin"}),
                             ("en cuenta_2 (Everest)", {"usuario": "evil2", "clave": "zz1234", "cuenta_id": "cuenta_2", "rol": "operario"}),
                             ("con cuenta 'todas'", {"usuario": "evil3", "clave": "zz1234", "cuenta_id": "todas", "rol": "operario"})]:
        r = req("POST", "/api/auth/usuarios", headers=ht(TK_BA), json=cuerpo)
        check(f"admin de tienda NO puede crear {etiqueta}", r.status_code == 403 and not any(u["usuario"].startswith("evil") for u in json.load(open(os.path.join(DATA, "usuarios.json")))), r.text[:120])
    check("admin de tienda NO puede tocar usuarios de Everest",
          req("PUT", "/api/auth/usuarios/colector1/clave", headers=ht(TK_BA), json={"clave": "hack1234"}).status_code == 403
          and req("DELETE", "/api/auth/usuarios/admin", headers=ht(TK_BA)).status_code == 403
          and req("PUT", "/api/auth/usuarios/colector1", headers=ht(TK_BA), json={"cuenta_id": TID}).status_code == 403)
    check("el operario de tienda no gestiona usuarios",
          req("POST", "/api/auth/usuarios", headers=ht(TK_B), json={"usuario": "evil4", "clave": "zz1234", "cuenta_id": TID, "rol": "operario"}).status_code == 403)
    # paneles web de la tienda
    ses_b = requests.Session(); rb = ses_b.post(B + "/admin/usuarios", data={"usuario": "badmin", "clave": "bpw1"})
    check("panel de la tienda: entra y NO incrusta la clave de Everest", rb.status_code == 200 and KEY not in rb.text and "tk1." in rb.text, rb.text[:100])
    check("panel de la tienda: solo ve sus usuarios", "badmin" in rb.text and "colector1" not in rb.text and "sup2" not in rb.text)
    check("panel de la tienda: título con SU nombre", "Tienda Beta" in rb.text and "Everest" not in rb.text)
    for ruta in ["/estadisticas", "/config", "/etiquetas"]:
        r = ses_b.get(B + ruta)
        check(f"panel de la tienda {ruta}: carga, sin la clave de Everest ni datos ajenos", r.status_code == 200 and KEY not in r.text and "EV-CODIGO-77" not in r.text and "fake-everest" not in r.text, f"{r.status_code}")
    check("/etiquetas de la tienda: su etiqueta, no la de Everest", OID_B in ses_b.get(B + "/etiquetas").text and OID_E not in ses_b.get(B + "/etiquetas").text)
    # rutas fuera de la lista
    for m, ruta in [("GET", "/api/tokens_export"), ("GET", "/api/diagnostico"), ("GET", "/api/token_status"), ("GET", "/api/skus"),
                    ("GET", "/api/admin/compat-stats"), ("POST", "/api/forzar-carga"), ("GET", "/auth/logout?cuenta=cuenta_2"),
                    ("GET", "/api/diag-colecta"), ("POST", "/api/skus/limpiar_todo")]:
        r = req(m, ruta, headers=ht(TK_B))
        check(f"tienda nueva: {m} {ruta} bloqueada", r.status_code in (401, 403) and sin_marcadores_b("") , f"{r.status_code} {r.text[:80]}")

    # ═══ D. ATAQUES Y AISLAMIENTO ═════════════════════════════════════════════
    print("D) ataques, sesiones falsas y cruces entre tiendas")
    from itsdangerous import URLSafeTimedSerializer, TimestampSigner

    class Viejo(TimestampSigner):
        def get_timestamp(self): return int(time.time()) - 40 * 86400

    def tok(secret, usuario, tid, rol="operario", viejo=False):
        kw = {"signer": Viejo} if viejo else {}
        return "tk1." + URLSafeTimedSerializer(secret, salt="logibot-tk1", **kw).dumps({"t": tid, "u": usuario, "r": rol})

    casos = {"token inventado": "tk1.inventado.invalido.x", "firmado con otra clave": tok("otra-clave", "bop", TID),
             "vencido (40 días)": tok(SECRET, "bop", TID, viejo=True), "usuario inexistente": tok(SECRET, "fantasma", TID),
             "usuario de otra tienda": tok(SECRET, "bop", ""), "tienda cambiada": tok(SECRET, "colector1", TID),
             "alterado": TK_B[:-3] + ("aaa" if not TK_B.endswith("aaa") else "bbb")}
    for n, t_ in casos.items():
        r = req("GET", "/api/pedidos", headers=ht(t_))
        check(f"token {n} → 401 (nunca cae a Everest)", r.status_code == 401 and "pedidos" not in r.text.replace("msg", ""), f"{r.status_code} {r.text[:80]}")
    r = req("GET", "/api/pedidos", headers=ht(TK_E))
    check("token válido de Everest = tienda primaria (todos sus pedidos)", j(r).get("total") == len(real))
    # cruces: clave heredada / sin clave hacia datos de la tienda nueva
    cruces = [("GET", f"/api/estado?usuario=bop&cuenta_id={TID}&canal=colecta"), ("GET", f"/api/estado?usuario=bop&cuenta_id=cuenta_2"),
              ("GET", f"/api/pedidos/impreso/{OID_B}"), ("GET", f"/api/etiqueta/{OID_B}"), ("GET", f"/api/etiqueta/{OID_B}/guardada"),
              ("POST", f"/api/pedidos/marcar_impreso/{OID_B}"), ("POST", f"/api/fase2/marcar-impresa/{OID_B}"),
              ("GET", f"/auth/logout?cuenta={TID}"), ("POST", f"/api/cuentas/{TID}/logout"), ("GET", "/api/sesion/recuperar/bop"),
              ("GET", f"/api/fase2/pedidos-completos?usuario=bop&cuenta_id={TID}")]
    for m, ruta in cruces:
        r = req(m, ruta, headers=hk())
        check(f"Everest (clave heredada) → {m} {ruta[:48]} bloqueado", r.status_code in (401, 403) and sin_marcadores_b(r.text), f"{r.status_code} {r.text[:80]}")
    r = req("POST", "/api/escanear", json={"sku": "SKU-B-1", "usuario": "bop", "cuenta_id": TID, "canal": "colecta"})
    check("Everest sin clave NO puede escanear en el lote de la tienda", r.status_code == 403, f"{r.status_code} {r.text[:80]}")
    r = req("POST", "/api/subir_estado", headers=hk(), json=dict(plb, usuario="bop", cuenta_id=TID))
    check("Everest NO puede subir un lote a nombre de la tienda", r.status_code == 403, f"{r.status_code}")
    # tienda nueva hacia Everest
    cruces_b = [("GET", f"/api/estado?usuario=colector1&cuenta_id=cuenta_2&canal=colecta"), ("GET", f"/api/etiqueta/{OID_E}/guardada"),
                ("GET", f"/api/etiqueta/{OID_E}"), ("GET", f"/api/pedidos/impreso/{oid0}"), ("POST", f"/api/pedidos/marcar_impreso/{oid0}"),
                ("GET", "/api/sesion/recuperar/colector1"), ("POST", "/api/cuentas/cuenta_2/logout")]
    for m, ruta in cruces_b:
        r = req(m, ruta, headers=ht(TK_B))
        check(f"tienda nueva → {m} {ruta[:48]} (dato de Everest) bloqueado", r.status_code in (401, 403) and "fake-everest" not in r.text and "EVEREST" not in r.text, f"{r.status_code} {r.text[:80]}")
    # falsificación de identidad de panel
    r = req("POST", "/api/auth/usuarios", headers=hk(**{"X-Panel-Rol": "admin", "X-Panel-Cuenta": "todas"}), json={"usuario": "pirata", "clave": "zz1234", "cuenta_id": "todas", "rol": "admin"})
    check("X-Panel-Rol falsificado NO crea admins", r.status_code == 403 and not any(u["usuario"] == "pirata" for u in json.load(open(os.path.join(DATA, "usuarios.json")))), r.text[:100])
    r = req("PUT", "/api/auth/usuarios/admin/clave", headers=hk(**{"X-Panel-Rol": "admin"}), json={"clave": "hackeado"})
    check("X-Panel-Rol falsificado NO cambia la clave del admin", r.status_code == 403 and j(req("POST", "/api/auth/login", json={"usuario": "admin", "clave": "1234"})).get("ok"))
    check("la lista de usuarios (clave heredada) no muestra la tienda nueva", not any(u["cuenta_id"] == TID for u in j(req("GET", "/api/auth/usuarios", headers=hk()))["usuarios"]))
    check("admin/tiendas sin credenciales de admin general → 403", req("GET", "/api/admin/tiendas", headers=ht(TK_BA)).status_code in (401, 403))
    # webhook: orden de usuario desconocido no se asigna a ninguna tienda
    n0 = len(j(req("GET", "/api/pedidos"))["pedidos"])
    req("POST", "/webhook/ml", json={"topic": "orders_v2", "resource": "/orders/1", "user_id": "999999"})
    time.sleep(3)
    check("webhook de usuario desconocido no cambia los pedidos", len(j(req("GET", "/api/pedidos"))["pedidos"]) == n0)
    # tienda suspendida: sus tokens dejan de valer
    j(req("POST", f"/api/admin/tiendas/{TID}/estado", headers=ADM, json={"estado": "suspendida"}))
    check("tienda suspendida: el token deja de valer", req("GET", "/api/pedidos", headers=ht(TK_B)).status_code == 403)
    check("tienda suspendida: no puede iniciar sesión", req("POST", "/api/auth/login", json={"usuario": "bop", "clave": "bpw2"}).status_code == 403)
    check("Everest sigue idéntico con la tienda suspendida", j(req("GET", "/api/pedidos"))["total"] == len(real))
    j(req("POST", f"/api/admin/tiendas/{TID}/estado", headers=ADM, json={"estado": "activa"}))
    check("reactivada: vuelve a entrar", req("GET", "/api/pedidos", headers=ht(TK_B)).status_code == 200)

    # ═══ E. PANTALLAS DE LA APP DE ESCRITORIO ════════════════════════════════
    print("E) pantallas de la app (nada de 'Railway')")
    sys.path.insert(0, REPO_DEFAULT)
    import tkinter as tk
    import app_deposito as A
    A.RAILWAY_URL = B

    def textos(w, acc=None):
        acc = acc if acc is not None else []
        for k in ("text", "title"):
            try:
                v = w.cget(k) if k == "text" else w.title()
                if v: acc.append(str(v))
            except Exception:
                pass
        for h in w.winfo_children():
            textos(h, acc)
        return acc

    root = tk.Tk(); root.withdraw()
    A.UI.apply_ctk_defaults(); A.UI.style_ttk(root)
    w = A.VentanaLogin(root, {"servidor_nube": B}, lambda u: None)
    w._ping_conexion(); w.update(); time.sleep(0.3); w.update()
    tx = textos(w)
    check("login: muestra '● Conectado' sin nombrar a nadie", any(t.strip() == "● Conectado" for t in tx), str([t for t in tx if "onect" in t]))
    check("login: ni rastro de 'Railway'", not any("railway" in t.lower() for t in tx), str([t for t in tx if "railway" in t.lower()]))
    w.destroy()
    for rol, plat in [("operario", False), ("supervisor", False), ("admin", False), ("admin", True)]:
        c = A.VentanaConfiguracion(root, {"servidor_nube": B, "clave_nube": KEY}, lambda cfg: None, rol=rol, plataforma=plat)
        c.update()
        def visibles(w_, acc=None):
            acc = acc if acc is not None else []
            if not w_.winfo_manager() and w_ is not c and not isinstance(w_, tk.Toplevel):
                return acc
            for k in ("text",):
                try:
                    v = w_.cget(k)
                    if v: acc.append(str(v))
                except Exception: pass
            for h in w_.winfo_children():
                if h.winfo_manager() or isinstance(h, tk.Toplevel): visibles(h, acc)
            return acc
        tx = visibles(c)
        malos = [t for t in tx if "railway" in t.lower()]
        if plat:
            check(f"Config ({rol}, plataforma): ve el servidor y los tokens", any(t == "SERVIDOR" for t in tx) and any("Guardar tokens" in t for t in tx), str(tx[:12]))
        else:
            check(f"Config ({rol}): oculta el servidor y los tokens", not any(t == "SERVIDOR" for t in tx) and not any("Guardar tokens" in t for t in tx) and not any(t.startswith("URL del servidor") for t in tx), str(tx[:14]))
        check(f"Config ({rol}): ni rastro de 'Railway' a la vista", not malos, str(malos))
        check(f"Config ({rol}): el guardado sigue leyendo sus campos", hasattr(c, "entry_nube") and c.entry_nube.get() == B)
        c.destroy()
    root.destroy()
finally:
    srv.kill()
print(f"\nRESULTADO INTEGRAL: {ok_n} ok, {fail_n} fallas")
for f in fallas: print("  ✗", f)
sys.exit(1 if fail_n else 0)

