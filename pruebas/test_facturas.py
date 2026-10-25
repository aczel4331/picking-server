"""Facturas → ML de punta a punta: servidor LOCAL + un Mercado Libre FALSO local (nada real).
Sube PDFs de CFE, verifica a qué venta van, las dudas, responder sí/otra/no, quitar, modo prueba
y que una tienda nueva no vea las facturas de Everest."""
import io, os, sys, json, time, shutil, subprocess, threading, base64
from datetime import datetime, timedelta
import requests

sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(S_DIR)
PORT, ML_PORT = "5151", 5152
B = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(S_DIR, "data_facturas")
K = {"X-API-Key": "everest2024"}
ok_n = fail_n = 0


def check(n, c, extra=""):
    global ok_n, fail_n
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f"   → {extra}"))
    if c: ok_n += 1
    else: fail_n += 1


# ── Mercado Libre falso ───────────────────────────────────────────────────────
from flask import Flask, request, jsonify
from werkzeug.serving import make_server

SELLER = "502960739"     # user_id de cuenta_2 en run_srv_pedidos.py


def _dia(d, mes="2026-05"):
    return f"{mes}-{d:02d}T10:00:00.000-03:00"


def _orden(oid, total, dia, buyer, pack=None, nombre=None, pagado=None, mes="2026-05"):
    b = {"id": buyer, "nickname": f"NICK{buyer}", "billing_info": {"id": f"BI{oid}"}}
    if nombre:
        b["first_name"], b["last_name"] = nombre
    return {"id": oid, "pack_id": pack, "status": "paid", "total_amount": total,
            "paid_amount": pagado if pagado is not None else total, "currency_id": "UYU",
            "date_created": _dia(dia, mes), "buyer": b}


ORDENES = [                                                    # ventas de mayo 2026 (día del mes)
    _orden(1001, 1600, 20, 1, nombre=("Jose", "Perez")),
    _orden(1002, 990, 15, 1, nombre=("Jose", "Perez")),
    _orden(1003, 1500, 22, 1, pack=2000003, nombre=("Jose", "Perez")),
    _orden(1004, 1000, 22, 1, pack=2000003, nombre=("Jose", "Perez")),
    _orden(1005, 1600, 21, 2),                                 # Ana: el nombre solo viene en billing-info
    _orden(1006, 500, 17, 3, nombre=("Luis", "Rodriguez")),
    _orden(1007, 500, 23, 3, nombre=("Luis", "Rodriguez")),
    _orden(1008, 800, 19, 4, nombre=("Marta", "Silva")),       # ya tiene factura en ML
    _orden(1009, 1200, 24, 5, nombre=("Carlos", "Diaz")),
    # el ejemplo del dueño: Juan Andrés compró 4 veces en mayo (la del 10 de mayo fue de $450)…
    _orden(1101, 450, 10, 6, nombre=("Juan", "Andres")),
    _orden(1102, 1200, 12, 6, nombre=("Juan", "Andres")),
    _orden(1103, 300, 18, 6, nombre=("Juan", "Andres")),
    _orden(1104, 980, 25, 6, nombre=("Juan", "Andres")),
    # …y otra de $450 en abril, que solo cuenta si se elige abril
    _orden(1105, 450, 20, 6, nombre=("Juan", "Andres"), mes="2026-04"),
    _orden(1010, 777, 26, 7, nombre=("Pedro", "Gomez")),        # se elige a mano en modo prueba
    _orden(1011, 650, 5, 8, nombre=("Rosa", "Diaz")),           # Rosa: dos compras iguales y la más
    _orden(1012, 650, 27, 8, nombre=("Rosa", "Diaz")),          # reciente ya tiene factura en ML
]
BILLING = {"BI1005": ("Ana", "Gómez")}
FISCAL = {"1008": [{"id": "previa", "file_type": "application/pdf"}],
          "1012": [{"id": "a_mano", "file_type": "application/pdf"}]}
POSTS, DELETES = [], []
FALLA = {"search": False}         # simula que /orders/search de ML no responde

ml = Flask("ml_falso")


@ml.route("/orders/search")
def _search():
    if FALLA["search"]:
        return jsonify({"message": "internal error"}), 500
    if request.args.get("seller") != SELLER:
        return jsonify({"results": [], "paging": {"total": 0}})
    desde = datetime.fromisoformat(request.args["order.date_created.from"])
    hasta = datetime.fromisoformat(request.args["order.date_created.to"])
    sel = [o for o in ORDENES if desde <= datetime.fromisoformat(o["date_created"]) <= hasta]
    off, lim = int(request.args.get("offset", 0)), int(request.args.get("limit", 50))
    return jsonify({"results": sel[off:off + lim], "paging": {"total": len(sel)}})


@ml.route("/orders/<oid>")
def _orden_get(oid):
    o = next((o for o in ORDENES if str(o["id"]) == oid), None)
    return (jsonify(o), 200) if o else (jsonify({"error": "not_found"}), 404)


@ml.route("/orders/billing-info/MLU/<bid>")
def _billing(bid):
    o = next((o for o in ORDENES if o["buyer"]["billing_info"]["id"] == bid), None)
    nom = BILLING.get(bid) or (o and (o["buyer"].get("first_name"), o["buyer"].get("last_name"))) or ("", "")
    return jsonify({"buyer": {"billing_info": {"name": nom[0], "last_name": nom[1],
                                               "identification": {"type": "CI", "number": "1234567"}}}})


@ml.route("/packs/<pid>/fiscal_documents", methods=["GET", "POST", "DELETE"])
def _fiscal(pid):
    if request.method == "GET":
        if pid in FISCAL:
            return jsonify({"pack_id": pid, "fiscal_documents": FISCAL[pid]})
        return jsonify({"message": "does not exist", "status": 404}), 404
    if request.method == "DELETE":
        DELETES.append(pid)
        FISCAL.pop(pid, None)
        return jsonify({"message": "deleted"})
    f = request.files.get("fiscal_document")
    if not f or not f.filename:
        return jsonify({"message": "File cannot be empty", "status": 400}), 400
    if pid in FISCAL:
        return jsonify({"message": "a file already exists", "status": 409}), 409
    POSTS.append(pid)
    FISCAL[pid] = [{"id": f"doc_{pid}", "file_type": f.mimetype}]
    return jsonify({"ids": [f"doc_{pid}"]})


import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
ml_srv = make_server("127.0.0.1", ML_PORT, ml, threaded=True)
threading.Thread(target=ml_srv.serve_forever, daemon=True).start()


# ── PDFs de prueba ────────────────────────────────────────────────────────────
def factura(nombre, total, numero, serie="A", fecha="31/05/2026", tipo="e-Ticket"):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for ln in ["EVEREST SAS   RUT EMISOR 214455660011", tipo, f"Serie: {serie}   Nro: {numero}",
               f"Fecha: {fecha}", f"Nombre: {nombre}", "Documento: 1.234.567-8",
               "Remera algodon   1   1.000,00", f"TOTAL A PAGAR  $ {total}"]:
        c.drawString(40, y, ln)
        y -= 16
    c.save()
    return buf.getvalue()


def subir(*pdfs, mes="2026-05"):
    files = [("factura", (f"f{i}.pdf", p, "application/pdf")) for i, p in enumerate(pdfs)]
    return SES.post(B + "/api/facturas/subir", headers=K, files=files, data={"mes": mes}, timeout=60).json()


# ── Servidor local ────────────────────────────────────────────────────────────
shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
           {"usuario": "sup", "clave": "s1", "nombre": "Supervisor", "cuenta_id": "cuenta_2", "rol": "supervisor"},
           {"usuario": "sup2", "clave": "s2", "nombre": "Supervisor 2", "cuenta_id": "cuenta_2", "rol": "supervisor"},
           {"usuario": "op", "clave": "o1", "nombre": "Operario", "cuenta_id": "cuenta_2", "rol": "operario"}],
          open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO,
           APP_SECRET_KEY="s" * 40, ML_API_URL=f"http://127.0.0.1:{ML_PORT}", ML_APP_ID="x", ML_SECRET_KEY="x",
           PYTHONIOENCODING="utf-8")
env.pop("PEDIDOS_SNAP", None)
srv = subprocess.Popen([sys.executable, os.path.join(S_DIR, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S_DIR, "facturas.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(0.5)

    # Facturación es solo para supervisores: las llamadas van con la sesión del panel de un supervisor
    SES = requests.Session(); SES.post(B + "/admin/usuarios", data={"usuario": "sup", "clave": "s1"})

    print("solo supervisores")
    r = requests.get(B + "/api/facturas", headers=K)
    check("con la clave de la app sola (sin sesión de supervisor) → 403", r.status_code == 403, r.status_code)
    r = requests.post(B + "/api/facturas/modo", headers=K, json={"prueba": False})
    check("sin supervisor no se puede apagar el modo prueba", r.status_code == 403, r.status_code)
    check("supervisor con sesión → ok", SES.get(B + "/api/facturas", headers=K).json().get("ok"))
    ses_op = requests.Session(); ses_op.post(B + "/admin/usuarios", data={"usuario": "op", "clave": "o1"})
    check("el login del panel rechaza operarios (no reciben sesión para /facturacion)",
          ses_op.get(B + "/facturacion", allow_redirects=False).status_code in (301, 302))
    # un supervisor pasado a operario queda afuera aunque conserve la cookie de antes
    ADM_SES = requests.Session(); ADM_SES.post(B + "/admin/usuarios", data={"usuario": "admin", "clave": "1234"})
    ses_s2 = requests.Session(); ses_s2.post(B + "/admin/usuarios", data={"usuario": "sup2", "clave": "s2"})
    check("supervisor 2 entra", ses_s2.get(B + "/api/facturas", headers=K).status_code == 200)
    ADM_SES.put(B + "/api/auth/usuarios/sup2", headers=K, json={"rol": "operario"})
    check("supervisor pasado a operario → 403 aunque tenga la cookie",
          ses_s2.get(B + "/api/facturas", headers=K).status_code == 403)
    check("…y /facturacion lo manda al login",
          ses_s2.get(B + "/facturacion", allow_redirects=False).status_code in (301, 302))

    print("modo prueba (encendido al empezar)")
    r = subir(factura("José Pérez", "1.600,00", "1"))["resultados"][0]
    check("Jose 1600 → simulada en la venta 1001", r["estado"] == "simulada" and r["pack_id"] == "1001", r)
    r = subir(factura("Pedro Gomez", "800,00", "30"))["resultados"][0]
    check("Pedro Gomez $800 (compró por $777) → duda", r["estado"] == "duda", r)
    r = SES.post(B + "/api/facturas/responder", headers=K,
                 json={"clave": r["clave"], "accion": "otra", "pack_id": "1010"}).json()
    check("en modo prueba se elige a mano la venta 1010 → simulada", r["ok"] and r["factura"]["estado"] == "simulada", r)
    check("modo prueba no sube nada a ML", POSTS == [], POSTS)

    print("apagar modo prueba y subir las simuladas")
    SES.post(B + "/api/facturas/modo", headers=K, json={"prueba": False})
    r = SES.post(B + "/api/facturas/subir-simuladas", headers=K, timeout=60).json()
    check("las simuladas se cargan en la venta decidida (la automática 1001 y la elegida a mano 1010)",
          r["ok"] and all(x["estado"] == "subida" for x in r["resultados"]) and sorted(POSTS) == ["1001", "1010"],
          (r, POSTS))

    print("lote: mismo comprador, montos distintos")
    rs = subir(factura("JOSE PEREZ", "990,00", "2"), factura("Pérez, José", "2.500,00", "3"))["resultados"]
    check("990 → 1002", rs[0]["estado"] == "subida" and rs[0]["pack_id"] == "1002", rs[0])
    check("2500 (pack de 2 órdenes) → pack 2000003", rs[1]["estado"] == "subida" and rs[1]["pack_id"] == "2000003", rs[1])

    r = subir(factura("José Pérez", "1.600,00", "1"))["resultados"][0]
    check("la misma factura otra vez no se vuelve a subir", r.get("repetida") and POSTS.count("1001") == 1, r)

    print("dos compras iguales del mismo comprador")
    rs = subir(factura("Luis Rodríguez", "500,00", "4"), factura("Luis Rodriguez", "500,00", "5"))["resultados"]
    check("dos facturas iguales → dos ventas distintas",
          {rs[0].get("pack_id"), rs[1].get("pack_id")} == {"1006", "1007"} and all(x["estado"] == "subida" for x in rs), rs)

    print("filtro por mes (ejemplo del dueño)")
    r = subir(factura("Juan Andrés", "450,00", "20"))["resultados"][0]
    check("Juan Andrés $450, mes mayo → la venta del 10 de mayo (1101) entre sus 4 compras",
          r["estado"] == "subida" and r["pack_id"] == "1101" and r["mes"] == "2026-05", r)
    r = subir(factura("Juan Andrés", "450,00", "21"), mes="2026-04")["resultados"][0]
    check("Juan Andrés $450, mes abril → la de abril (1105)", r["estado"] == "subida" and r["pack_id"] == "1105", r)
    r = subir(factura("Juan Andrés", "980,00", "22"), mes="2026-04")["resultados"][0]
    check("Juan Andrés $980 con mes abril → duda (esa compra es de mayo)", r["estado"] == "duda"
          and "1104" not in POSTS, r)
    r = subir(factura("Juan Andrés", "980,00", "22"), mes="2026-05")["resultados"][0]
    check("la misma factura con el mes correcto → 1104", r["estado"] == "subida" and r["pack_id"] == "1104", r)
    r = SES.post(B + "/api/facturas/subir", headers=K, data={"mes": "2026-13"},
                      files=[("factura", ("x.pdf", factura("X Y", "1,00", "99"), "application/pdf"))])
    check("mes inválido → 400", r.status_code == 400, r.text)
    for mes_malo in ("0000-01", "2019-12", "2999-03"):
        r = SES.post(B + "/api/facturas/subir", headers=K, data={"mes": mes_malo},
                     files=[("factura", ("x.pdf", factura("X Y", "1,00", "99"), "application/pdf"))])
        check(f"mes {mes_malo} (fuera de rango o futuro) → 400", r.status_code == 400, r.text)

    print("tipos de CFE: misma serie y número")
    r = subir(factura("Juan Andrés", "1.200,00", "20", tipo="e-Factura"))["resultados"][0]
    check("e-Factura A-20 no se confunde con el e-Ticket A-20 ya subido → 1102",
          r["estado"] == "subida" and r["pack_id"] == "1102" and not r.get("repetida"), r)

    print("dudas")
    r = subir(factura("Marta Silva", "800,00", "6"))["resultados"][0]
    check("venta que ya tiene factura en ML → duda, sin subir", r["estado"] == "duda" and "1008" not in POSTS
          and "#1008" in r["motivo"], r)
    r = subir(factura("Pedro Martinez", "1.600,00", "7"))["resultados"][0]
    check("monto coincide pero el nombre no → duda", r["estado"] == "duda"
          and any(c["pack_id"] == "1005" for c in r["candidatas"]), r)
    clave_pedro = r["clave"]
    r = subir(factura("Ana Gomez", "1.234,00", "8"))["resultados"][0]
    check("nombre (de billing-info) coincide, monto no → duda con propuesta 1005", r["estado"] == "duda"
          and r["candidatas"] and r["candidatas"][0]["pack_id"] == "1005", r)
    clave_ana = r["clave"]
    r = subir(factura("Nadie Conocido", "77,00", "9"))["resultados"][0]
    clave_nadie = r["clave"]
    check("sin coincidencias → duda", r["estado"] == "duda", r)
    n_posts = len(POSTS)
    d = SES.get(B + "/api/facturas", headers=K).json()
    check("4 dudas pendientes", d["dudas"] == 4 and len(d["dudas_lista"]) == 4,
          [x["factura"]["nombre"] for x in d["dudas_lista"]])

    print("responder dudas")
    r = SES.post(B + "/api/facturas/responder", headers=K, json={"clave": clave_ana, "accion": "si"}).json()
    check("Sí → sube a la propuesta 1005", r["ok"] and r["factura"]["estado"] == "subida" and POSTS[-1] == "1005", r)
    r = SES.post(B + "/api/facturas/responder", headers=K,
                      json={"clave": clave_pedro, "accion": "otra", "pack_id": "1009"}).json()
    check("Otra (escrita a mano) → sube a 1009", r["ok"] and POSTS[-1] == "1009", r)
    r = SES.post(B + "/api/facturas/responder", headers=K,
                      json={"clave": clave_nadie, "accion": "otra", "pack_id": "1001"}).json()
    check("Otra a una venta ya facturada → rechazada", not r["ok"] and len(POSTS) == n_posts + 2, r)
    r = SES.post(B + "/api/facturas/responder", headers=K, json={"clave": clave_nadie, "accion": "no"}).json()
    check("No → descartada sin subir", r["ok"] and r["factura"]["estado"] == "descartada" and len(POSTS) == n_posts + 2, r)
    d = SES.get(B + "/api/facturas", headers=K).json()
    check("queda 1 duda (Marta)", d["dudas"] == 1 and d["dudas_lista"][0]["factura"]["nombre"] == "Marta Silva", d)
    r = SES.post(B + "/api/facturas/responder", headers=K, json={"clave": d["dudas_lista"][0]["clave"],
                                                                 "accion": "si"})
    check("«Sí» sin venta propuesta → 400 con mensaje claro", r.status_code == 400 and "propuesta" in r.json()["msg"],
          r.text)

    print("venta que coincide ya facturada en ML: nunca se carga sola en otra compra del comprador")
    r = subir(factura("Rosa Diaz", "650,00", "40"))["resultados"][0]
    check("Rosa $650: la compra más reciente (1012) ya tiene factura → duda proponiendo 1011, sin subir",
          r["estado"] == "duda" and "#1012" in r["motivo"] and r["candidatas"]
          and r["candidatas"][0]["pack_id"] == "1011" and "1011" not in POSTS, r)

    print("Mercado Libre no responde")
    FALLA["search"] = True
    r = subir(factura("Lucia Mendez", "300,00", "41"), mes="2026-03")["resultados"][0]
    check("sin respuesta de ML → error claro, no una duda falsa", r["estado"] == "error"
          and "No se pudieron consultar" in r["motivo"], r)
    FALLA["search"] = False
    r = subir(factura("Lucia Mendez", "300,00", "41"), mes="2026-03")["resultados"][0]
    check("al volver ML, la misma factura se vuelve a buscar", r["estado"] == "duda" and not r.get("repetida")
          and "marzo 2026" in r["motivo"], r)

    print("quitar")
    lista = SES.get(B + "/api/facturas", headers=K).json()["facturas"]
    e = next(x for x in lista if x.get("pack_id") == "1009")
    r = SES.post(B + "/api/facturas/quitar", headers=K, json={"clave": e["clave"]}).json()
    check("Quitar → DELETE en ML", r["ok"] and DELETES == ["1009"], (r, DELETES))

    print("archivos inválidos")
    r = subir(b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10))["resultados"][0]
    check("PDF de más de 1 MB → error", r["estado"] == "error" and "1 MB" in r["motivo"], r)
    r = subir(b"hola")["resultados"][0]
    check("archivo que no es PDF → error", r["estado"] == "error", r)

    print("tiendas y panel")
    ADM = {"X-Admin-Usuario": "admin", "X-Admin-Clave": "1234"}
    from PIL import Image
    bb = io.BytesIO(); Image.new("RGBA", (60, 20), (1, 2, 3, 255)).save(bb, "PNG")
    TID = requests.post(B + "/api/admin/tiendas", headers=ADM,
                        json={"nombre": "Tienda Fact", "logo_b64": base64.b64encode(bb.getvalue()).decode()}).json()["tienda"]["id"]
    ses = requests.Session(); ses.post(B + "/admin/usuarios", data={"usuario": "admin", "clave": "1234"})
    ses.post(B + "/api/auth/usuarios", headers=K,
             json={"usuario": "fop", "clave": "fpw2", "nombre": "Op F", "cuenta_id": TID, "rol": "supervisor"})
    tok = requests.post(B + "/api/auth/login", json={"usuario": "fop", "clave": "fpw2"}).json()["token"]
    r = requests.get(B + "/api/facturas", headers={"X-API-Key": tok}).json()
    check("tienda nueva no ve las facturas de Everest", r["ok"] and r["facturas"] == [], r)
    r = requests.post(B + "/api/facturas/subir", headers={"X-API-Key": tok},
                      data={"mes": "2026-05"},
                      files=[("factura", ("x.pdf", factura("José Pérez", "990,00", "50"), "application/pdf"))]).json()
    check("tienda nueva sin ML conectado no usa las ventas de Everest",
          r["resultados"][0]["estado"] == "error" and "conectada" in r["resultados"][0]["motivo"], r)
    html = ses.get(B + "/facturacion").text
    check("/facturacion muestra la página de facturas", "fact-drop" in html and "mes-titulo" in html
          and "fact-dudas" in html and "__CLAVE_PANEL__" not in html)
    check("/facturacion no dice Railway", "railway" not in html.lower())
    cfg_html = ses.get(B + "/config").text
    check("/config ya no tiene las facturas, solo la pestaña", "fact-drop" not in cfg_html
          and 'href="/facturacion"' in cfg_html)
    check("pestaña Facturación en usuarios y estadísticas",
          all('href="/facturacion"' in ses.get(B + u).text for u in ("/admin/usuarios", "/estadisticas")))
    check("sin sesión /facturacion lleva al login",
          requests.get(B + "/facturacion", allow_redirects=False).status_code in (301, 302))
    ses_t = requests.Session(); ses_t.post(B + "/admin/usuarios", data={"usuario": "fop", "clave": "fpw2"})
    ht = ses_t.get(B + "/facturacion")
    check("una tienda nueva también puede abrir /facturacion", ht.status_code == 200 and "fact-drop" in ht.text,
          ht.status_code)
    for metodo, ruta in (("get", "/api/facturas"), ("post", "/api/facturas/modo"), ("post", "/api/facturas/quitar")):
        r = getattr(ses_t, metodo)(B + ruta, headers=K, json={"prueba": False} if ruta.endswith("modo") else {})
        check(f"supervisor de otra tienda + clave vieja de Everest → 403 en {ruta}", r.status_code == 403, r.status_code)
finally:
    srv.terminate()
    ml_srv.shutdown()

print(f"\n{ok_n} OK · {fail_n} FALLAS")
sys.exit(1 if fail_n else 0)
