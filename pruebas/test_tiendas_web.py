"""Tiendas desde el servidor: el admin crea la tienda y su supervisor en la pestaña Tiendas, el
supervisor entra al panel, conecta su Mercado Libre con el botón y usa Facturación (plan "solo
facturación"). Servidor LOCAL + Mercado Libre FALSO local; nada real. Lo de la app de escritorio
(/api/admin/tiendas con X-Admin-*) tiene que seguir igual."""
import io, os, re, sys, json, time, shutil, subprocess, threading, base64
from urllib.parse import urlparse, parse_qs
import requests

sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(S_DIR)
PORT, ML_PORT = "5154", 5155
B = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(S_DIR, "data_tiendas_web")
ok_n = fail_n = 0


def check(n, c, extra=""):
    global ok_n, fail_n
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f"   → {extra}"))
    if c: ok_n += 1
    else: fail_n += 1


# ── Mercado Libre falso (OAuth + ventas de la tienda nueva) ───────────────────
from flask import Flask, request, jsonify
from werkzeug.serving import make_server
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

ML_UID = "777000"
ORDENES = [{"id": 9001, "pack_id": None, "status": "paid", "total_amount": 1500, "paid_amount": 1500,
            "currency_id": "UYU", "date_created": time.strftime("%Y-%m-05T10:00:00.000-03:00"),
            "buyer": {"id": 1, "nickname": "COMPRA1", "first_name": "Carla", "last_name": "Suarez",
                      "billing_info": {"id": "BI9001"}}}]
ml = Flask("ml_falso")


@ml.route("/oauth/token", methods=["POST"])
def _token():
    return jsonify({"access_token": "AT-TIENDA", "refresh_token": "RT-TIENDA", "expires_in": 21600})


@ml.route("/users/me")
def _me():
    return jsonify({"id": int(ML_UID), "nickname": "TIENDA_FACT"})


@ml.route("/orders/search")
def _search():
    if request.args.get("seller") != ML_UID:
        return jsonify({"results": [], "paging": {"total": 0}})
    return jsonify({"results": ORDENES, "paging": {"total": len(ORDENES)}})


@ml.route("/orders/<oid>")
def _orden(oid):
    o = next((o for o in ORDENES if str(o["id"]) == oid), None)
    return (jsonify(o), 200) if o else (jsonify({}), 404)


@ml.route("/orders/billing-info/MLU/<bid>")
def _billing(bid):
    return jsonify({"buyer": {"billing_info": {"name": "Carla", "last_name": "Suarez"}}})


@ml.route("/packs/<pid>/fiscal_documents", methods=["GET"])
def _fiscal(pid):
    return jsonify({"message": "does not exist"}), 404


ml_srv = make_server("127.0.0.1", ML_PORT, ml, threaded=True)
threading.Thread(target=ml_srv.serve_forever, daemon=True).start()


def factura(nombre, total, numero):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); c = canvas.Canvas(buf); y = 800
    for ln in ["TIENDA FACT  RUT EMISOR 210000000011", "e-Ticket", f"Serie: B   Nro: {numero}",
               time.strftime("Fecha: 20/%m/%Y"), f"Nombre: {nombre}", f"TOTAL A PAGAR  $ {total}"]:
        c.drawString(40, y, ln); y -= 16
    c.save()
    return buf.getvalue()


def logo_png():
    from PIL import Image
    bb = io.BytesIO(); Image.new("RGBA", (40, 20), (1, 2, 3, 255)).save(bb, "PNG")
    return base64.b64encode(bb.getvalue()).decode()


def sesion(usuario, clave):
    s = requests.Session()
    r = s.post(B + "/admin/usuarios", data={"usuario": usuario, "clave": clave}, allow_redirects=False)
    return s, r


# ── Servidor local ────────────────────────────────────────────────────────────
shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
           {"usuario": "sup", "clave": "s1", "nombre": "Supervisor Everest", "cuenta_id": "cuenta_2", "rol": "supervisor"}],
          open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, MULTI_TIENDA="1",
           APP_SECRET_KEY="s" * 40, ML_API_URL=f"http://127.0.0.1:{ML_PORT}", ML_AUTH_URL=f"http://127.0.0.1:{ML_PORT}",
           ML_REDIRECT_URI=f"{B}/auth/callback", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
env.pop("PEDIDOS_SNAP", None)
srv = subprocess.Popen([sys.executable, os.path.join(S_DIR, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S_DIR, "tiendas_web.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(0.5)

    print("pestaña Tiendas: solo el admin general")
    adm, _ = sesion("admin", "1234")
    sup, _ = sesion("sup", "s1")
    r = adm.get(B + "/admin/tiendas")
    check("el admin abre /admin/tiendas", r.status_code == 200 and "Nueva tienda" in r.text
          and 'href="/admin/tiendas"' in r.text and "__BARRA_PANEL__" not in r.text, r.status_code)
    check("el supervisor de Everest no la abre",
          sup.get(B + "/admin/tiendas", allow_redirects=False).status_code in (301, 302))
    check("…ni ve la pestaña Tiendas", 'href="/admin/tiendas"' not in sup.get(B + "/config").text)
    check("sin sesión la API de tiendas → 403", requests.get(B + "/api/panel/tiendas").status_code == 403)
    check("con sesión de supervisor → 403", sup.get(B + "/api/panel/tiendas").status_code == 403)

    print("crear tienda + supervisor")
    r = adm.post(B + "/api/panel/tiendas", json={"nombre": "Tienda Fact", "plan": "facturacion",
                                                 "usuario": "ventas1", "clave": "clave123", "nombre_usuario": "Ventas"}).json()
    check("tienda 'solo facturación' creada sin logo, pendiente de ML, con su supervisor",
          r.get("ok") and r["tienda"]["plan"] == "facturacion" and r["tienda"]["estado"] == "pendiente_ml"
          and r["tienda"]["supervisores"] == ["ventas1"], r)
    TID = r["tienda"]["id"]
    r = adm.post(B + "/api/panel/tiendas", json={"nombre": "Otra", "plan": "facturacion", "usuario": "ventas1", "clave": "clave123"})
    check("usuario repetido → 409 (y no crea la tienda)", r.status_code == 409
          and sum(t["nombre"] == "Otra" for t in adm.get(B + "/api/panel/tiendas").json()["tiendas"]) == 0, r.text)
    r = adm.post(B + "/api/panel/tiendas", json={"nombre": "Completa", "plan": "completo", "usuario": "comp1", "clave": "clave123"})
    check("plan Completo sin logo → 400", r.status_code == 400 and "logo" in r.json()["msg"], r.text)
    r = adm.post(B + "/api/panel/tiendas", json={"nombre": "Corta", "plan": "facturacion", "usuario": "corta", "clave": "123"})
    check("clave corta → 400", r.status_code == 400, r.text)

    print("la app de escritorio sigue igual")
    ADM = {"X-Admin-Usuario": "admin", "X-Admin-Clave": "1234"}
    r = requests.post(B + "/api/admin/tiendas", headers=ADM, json={"nombre": "Desde la app"})
    check("app: sin logo → 400 con el mismo mensaje", r.status_code == 400
          and r.json()["msg"] == "Para crear la tienda hay que elegir su logo", r.text)
    r = requests.post(B + "/api/admin/tiendas", headers=ADM, json={"nombre": "Desde la app", "logo_b64": logo_png()}).json()
    check("app: con logo → creada, plan completo", r.get("ok") and r["tienda"]["plan"] == "completo", r)
    TID_APP = r["tienda"]["id"]
    r = requests.post(B + f"/api/admin/tiendas/{TID_APP}/invitacion", headers=ADM).json()
    check("app: enlace de invitación igual que antes", r.get("ok") and "/auth/login?invite=" in r["link"], r)
    check("app: listado con headers sigue andando",
          any(t["id"] == TID for t in requests.get(B + "/api/admin/tiendas", headers=ADM).json()["tiendas"]))

    print("el supervisor de la tienda entra al panel")
    ven, r = sesion("ventas1", "clave123")
    check("al entrar va directo a Facturación", r.status_code == 302 and r.headers["Location"].endswith("/facturacion"),
          (r.status_code, r.headers.get("Location")))
    html = ven.get(B + "/facturacion").text
    check("ve el botón Conectar Mercado Libre", "/facturacion/conectar-ml" in html and "Conectar Mercado Libre" in html)
    check("su barra tiene solo Facturación y Usuarios",
          'href="/facturacion"' in html and 'href="/admin/usuarios"' in html
          and 'href="/estadisticas"' not in html and 'href="/etiquetas"' not in html and 'href="/admin/tiendas"' not in html)
    for p in ("/estadisticas", "/etiquetas", "/config"):
        r = ven.get(B + p, allow_redirects=False)
        check(f"plan solo facturación: {p} → lo manda a Facturación",
              r.status_code == 302 and r.headers["Location"].endswith("/facturacion"), r.status_code)
    r = requests.post(B + "/api/auth/login", json={"usuario": "ventas1", "clave": "clave123"})
    check("la app de escritorio/celular no deja entrar a un usuario del plan facturación",
          r.status_code == 403 and "Facturación" in r.json()["msg"], r.text)
    check("no abre la pestaña Tiendas", ven.get(B + "/admin/tiendas", allow_redirects=False).status_code != 200)
    up = ven.get(B + "/admin/usuarios")
    check("su página de Usuarios ofrece crear supervisores", up.status_code == 200
          and 'value="supervisor"' in up.text and 'value="operario"' not in up.text, up.status_code)

    print("conectar Mercado Libre desde el botón")
    r = ven.get(B + "/facturacion/conectar-ml", allow_redirects=False)
    loc = r.headers.get("Location", "")
    check("el botón manda a autorizar en Mercado Libre", r.status_code == 302 and "/authorization?" in loc, loc)
    state = parse_qs(urlparse(loc).query).get("state", [""])[0]
    r = requests.get(B + "/auth/callback", params={"code": "CODIGO", "state": state}, allow_redirects=False)
    check("al volver de ML regresa a Facturación", r.status_code == 302
          and r.headers["Location"].endswith("/facturacion?ml=conectado"), (r.status_code, r.headers.get("Location")))
    t = next(t for t in adm.get(B + "/api/panel/tiendas").json()["tiendas"] if t["id"] == TID)
    check("la tienda quedó activa con su cuenta de ML", t["estado"] == "activa" and t["ml_conectado"]
          and t["nickname"] == "TIENDA_FACT", t)
    html = ven.get(B + "/facturacion").text
    check("ya no ve el aviso de conectar", "/facturacion/conectar-ml" not in html)
    m = re.search(r'const KEY\s*=\s*"([^"]+)"', html)
    KEY = {"X-API-Key": m.group(1) if m else ""}
    check("la página de la tienda trae su token propio (no la clave de Everest)", KEY["X-API-Key"].startswith("tk1."), KEY)

    print("Facturación con las ventas de SU cuenta")
    r = ven.post(B + "/api/facturas/subir", headers=KEY, data={"mes": time.strftime("%Y-%m")},
                 files=[("factura", ("f.pdf", factura("Carla Suarez", "1.500,00", "1"), "application/pdf"))]).json()
    res = (r.get("resultados") or [{}])[0]
    check("la factura encuentra la venta 9001 de la tienda (modo prueba)", res.get("estado") == "simulada"
          and res.get("pack_id") == "9001" and res.get("cuenta") == TID, r)
    r = requests.get(B + "/api/facturas", headers={"X-API-Key": "everest2024"})
    check("Everest no ve esa factura (sin sesión de supervisor ni token de la tienda)", r.status_code == 403, r.status_code)

    print("usuarios de la tienda")
    r = requests.post(B + "/api/auth/usuarios", headers=KEY,
                      json={"usuario": "ventas2", "clave": "clave456", "nombre": "Ventas 2", "rol": "supervisor"})
    check("el supervisor suma otro supervisor de su tienda", r.status_code == 200 and r.json()["usuario"]["cuenta_id"] == TID, r.text)
    r = requests.post(B + "/api/auth/usuarios", headers=KEY,
                      json={"usuario": "op9", "clave": "clave456", "nombre": "Op", "rol": "operario"})
    check("…pero no operarios (no hay picking en su plan)", r.status_code == 403, r.text)

    print("cambiar plan y suspender")
    r = adm.post(B + f"/api/panel/tiendas/{TID}/plan", json={"plan": "completo"}).json()
    check("el admin la pasa a Completo", r.get("ok") and r["tienda"]["plan"] == "completo", r)
    check("con plan Completo ya puede abrir Estadísticas", ven.get(B + "/estadisticas", allow_redirects=False).status_code == 200)
    adm.post(B + f"/api/panel/tiendas/{TID}/plan", json={"plan": "facturacion"})
    r = adm.post(B + f"/api/panel/tiendas/{TID}/estado", json={"estado": "suspendida"}).json()
    check("el admin la suspende", r.get("ok") and r["tienda"]["estado"] == "suspendida", r)
    r = ven.get(B + "/facturacion", allow_redirects=False)
    check("con la tienda suspendida, la sesión abierta queda afuera", "suspendida" in r.text and "fact-drop" not in r.text,
          r.status_code)
    _, r = sesion("ventas1", "clave123")
    check("…y no puede volver a entrar", "suspendida" in r.text and r.status_code == 200, r.status_code)
    adm.post(B + f"/api/panel/tiendas/{TID}/estado", json={"estado": "activa"})
    ven2, r = sesion("ventas1", "clave123")
    check("reactivada, entra de nuevo", r.status_code == 302, r.status_code)
    r = adm.post(B + "/api/panel/tiendas/cuenta_2/estado", json={"estado": "suspendida"})
    check("la tienda principal (Everest) no se puede suspender desde la web", r.status_code == 400, r.text)

    print("eliminar tiendas")
    elim = lambda tid, nombre: adm.post(B + f"/api/panel/tiendas/{tid}/eliminar", json={"confirmar": nombre})
    r = elim(TID, "Tienda Fact")
    check("una tienda activa no se elimina (primero hay que suspenderla)", r.status_code == 400
          and "suspend" in r.json()["msg"], r.text)
    adm.post(B + f"/api/panel/tiendas/{TID}/estado", json={"estado": "suspendida"})
    r = elim(TID, "otro nombre")
    check("sin escribir bien el nombre no se elimina", r.status_code == 400, r.text)
    check("sin sesión de admin no se elimina",
          requests.post(B + f"/api/panel/tiendas/{TID}/eliminar", json={"confirmar": "Tienda Fact"}).status_code == 403)
    check("el supervisor de la tienda no puede eliminarla",
          ven2.post(B + f"/api/panel/tiendas/{TID}/eliminar", json={"confirmar": "Tienda Fact"}).status_code == 403)
    r = elim(TID, "  tienda fact ")
    check("suspendida + nombre (sin importar mayúsculas) → eliminada con sus usuarios", r.status_code == 200
          and sorted(r.json()["eliminada"]["usuarios"]) == ["ventas1", "ventas2"], r.text)
    lista = adm.get(B + "/api/panel/tiendas").json()["tiendas"]
    check("ya no aparece en la lista", all(t["id"] != TID for t in lista))
    check("sus datos se guardaron aparte (no se destruyen)",
          any(d.startswith(TID + "_") for d in os.listdir(os.path.join(DATA, "tiendas_borradas")))
          and not os.path.exists(os.path.join(DATA, "tiendas", TID)))
    _, r = sesion("ventas1", "clave123")
    check("su usuario ya no entra", "fact-drop" not in r.text and r.status_code == 200, r.status_code)
    r = ven2.get(B + "/facturacion", allow_redirects=False)
    check("una sesión abierta de esa tienda queda afuera", "ya no existe" in r.text, r.status_code)
    check("su token de la app deja de servir",
          requests.get(B + "/api/facturas", headers=KEY).status_code in (401, 403))
    check("la tienda principal no se puede eliminar", elim("cuenta_2", "cuenta_2").status_code == 400)
    check("Everest sigue igual", adm.get(B + "/admin/usuarios").status_code == 200
          and any(t.get("primary") for t in adm.get(B + "/api/panel/tiendas").json()["tiendas"]))
finally:
    srv.terminate()
    ml_srv.shutdown()

print(f"\n{ok_n} OK · {fail_n} FALLAS")
sys.exit(1 if fail_n else 0)
