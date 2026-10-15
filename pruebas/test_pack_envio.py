"""Un envío (una etiqueta) = UN pedido con TODOS sus productos, venga por el refresco o por el webhook.
Antes el webhook guardaba cada orden suelta y pisaba al principal ya fusionado: la app pedía 4 productos
y la etiqueta del envío traía 16. Se prueba con las órdenes reales (foto de producción)."""
import os, sys, json, copy, random, tempfile, types, logging, threading, time
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
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

class Cap(logging.Handler):
    def __init__(self): super().__init__(); self.lineas = []
    def emit(self, r): self.lineas.append(r.getMessage())
cap = Cap(); S.logger.addHandler(cap); S.logger.setLevel(logging.DEBUG)

def it(sku, cant=1, item_id=None, color="", talle=""):
    return {"item_id": item_id or ("MLU" + sku), "titulo": "P " + sku, "sku": sku, "cantidad": cant,
            "color": color, "talle": talle, "unit_price": 10}

def orden(oid, ship, items, pack="PK1", cuenta="cuenta_2", **extra):
    p = {"order_id": str(oid), "pack_id": pack, "shipping_id": ship, "items": items, "total": 10 * len(items),
         "_cuenta": cuenta, "_nickname": "E", "comprador": "B", "fecha": "2026-10-14", "logistica": "", "estado_envio": "",
         "impreso": False, "tags": []}
    p.update(extra)
    return p

def uds(p): return sum(i["cantidad"] for i in p["items"])
def skus(p): return sorted((i["sku"], i["cantidad"]) for i in p["items"])

# ── 1. agrupado del refresco ────────────────────────────────────────────────────
print("1) agrupado por envío (refresco)")
g = S._agrupar_por_envio({"1": orden(1, "S1", [it("A")]), "2": orden(2, "S1", [it("B", 2)]), "3": orden(3, "S1", [it("C")]),
                          "4": orden(4, "S2", [it("A")])})
check("3 órdenes del mismo envío = 1 entrada con todo", list(g) == ["1", "4"] and skus(g["1"]) == [("A", 1), ("B", 2), ("C", 1)], g)
check("queda registrado qué órdenes lo componen", g["1"]["_orders_agrupadas"] == ["1", "2", "3"], g["1"].get("_orders_agrupadas"))
check("otro envío no se mezcla", skus(g["4"]) == [("A", 1)])

g = S._agrupar_por_envio({"1": orden(1, "S1", [it("A", 1)]), "2": orden(2, "S1", [it("A", 3)]), "3": orden(3, "S1", [it("A", 2, color="Rojo")])})
check("mismo producto en dos órdenes: SUMA la cantidad (antes la app la perdía)", g["1"]["items"][0]["cantidad"] == 4, g["1"]["items"])
check("misma SKU pero otro color: línea aparte", len(g["1"]["items"]) == 2 and uds(g["1"]) == 6, g["1"]["items"])

g = S._agrupar_por_envio({"1": orden(1, "S1", [it("", 1, item_id="MLU1")]), "2": orden(2, "S1", [it("", 1, item_id="MLU2")])})
check("dos productos distintos SIN SKU no se funden en una línea", len(g["1"]["items"]) == 2 and uds(g["1"]) == 2, g["1"]["items"])

g = S._agrupar_por_envio({"1": orden(1, "", [it("A")]), "2": orden(2, "", [it("B")]), "3": orden(3, "S9", [it("C")], pack="PK1")})
check("sin shipping_id no se agrupa; mismo pack con otro envío tampoco", sorted(g) == ["1", "2", "3"])

# ── 2. webhook, orden por orden ─────────────────────────────────────────────────
print("2) webhook: una orden por aviso, cualquier orden de llegada, avisos repetidos")
def vaciar(): S._pedidos_ml.clear()
vaciar()
ordenes = [orden(i, "S7", [it(f"P{i}", 1 + i % 2)]) for i in range(1, 8)]
total = sum(uds(o) for o in ordenes)
random.seed(4)
for _ in range(20):                       # 20 mezclas de orden de llegada, con avisos duplicados
    vaciar()
    cola = ordenes + random.sample(ordenes, 6)
    random.shuffle(cola)
    for o in cola:
        S._registrar_orden_webhook("cuenta_2", copy.deepcopy(o))
    assert len(S._pedidos_ml) == 1, S._pedidos_ml.keys()
    p = list(S._pedidos_ml.values())[0]
    if uds(p) != total or len(p["_orders_agrupadas"]) != 7:
        break
check("7 órdenes del mismo envío, 20 mezclas con avisos repetidos: 1 entrada, mismas unidades", len(S._pedidos_ml) == 1 and uds(p) == total and len(p["_orders_agrupadas"]) == 7,
      (len(S._pedidos_ml), uds(p), total, p.get("_orders_agrupadas")))

vaciar()
S._pedidos_ml.update(S._agrupar_por_envio({str(o["order_id"]): copy.deepcopy(o) for o in ordenes}))
antes = copy.deepcopy(list(S._pedidos_ml.values())[0])
res = [S._registrar_orden_webhook("cuenta_2", copy.deepcopy(o)) for o in ordenes]
despues = list(S._pedidos_ml.values())[0]
check("aviso de CADA orden sobre un envío ya agrupado por el refresco: no cambia nada", len(S._pedidos_ml) == 1 and uds(despues) == total and set(res) == {"ya_incluida"}, (res, uds(despues)))
check("(el principal NO queda con los items de una sola orden)", len(despues["items"]) == 7 and despues["items"] == antes["items"])

vaciar()
for o in ordenes[:3]:
    S._pedidos_ml[str(o["order_id"])] = copy.deepcopy(o)       # estado actual de producción: sueltas
S._registrar_orden_webhook("cuenta_2", copy.deepcopy(ordenes[3]))
check("entradas sueltas del mismo envío se consolidan al llegar otro aviso", len(S._pedidos_ml) == 1 and uds(list(S._pedidos_ml.values())[0]) == sum(uds(o) for o in ordenes[:4]),
      [(k, uds(v)) for k, v in S._pedidos_ml.items()])

vaciar()
ya = orden(1, "S1", [it("A")], logistica="self_service", estado_envio="ready_to_ship", substatus="ready_to_print")
S._pedidos_ml["1"] = ya
r = S._registrar_orden_webhook("cuenta_2", orden(1, "S1", [it("A")]))
check("aviso de una orden suelta que el refresco ya trajo: se conserva lo que sabía (logística/estado)", r == "existente" and S._pedidos_ml["1"] is ya and ya["substatus"] == "ready_to_print", r)

vaciar()
S._registrar_orden_webhook("cuenta_2", orden(1, "S1", [it("A")]))
S._registrar_orden_webhook("cuenta_2", orden(2, "S2", [it("B")]))
S._registrar_orden_webhook("otra", orden(3, "S1", [it("C")], cuenta="otra"))
check("envíos distintos y cuentas distintas no se mezclan", sorted(S._pedidos_ml) == ["1", "2", "3"])

# ── 3. flujo completo del webhook (consulta a ML simulada) ──────────────────────
print("3) _procesar_notificacion_orden con ML simulado")
class R:
    status_code = 200
    def __init__(self, j): self._j = j
    def json(self): return self._j
def orden_ml(oid, sku, q=1, ship=555):
    return {"status": "paid", "id": oid, "pack_id": 2000015, "date_created": "2026-10-14T10:00:00.000-04:00",
            "date_closed": "2026-10-14T10:01:00.000-04:00",
            "order_items": [{"item": {"id": "MLU" + sku, "title": "T" + sku, "seller_custom_field": sku}, "quantity": q, "unit_price": 10}],
            "shipping": {"id": ship}, "buyer": {"nickname": "B"}, "total_amount": 10, "currency_id": "UYU", "tags": []}
ML = {str(o): orden_ml(o, f"K{o}", 1 + o % 3) for o in range(901, 905)}
S.requests = types.SimpleNamespace(get=lambda url, **k: R(ML[url.rsplit("/", 1)[1]]), post=None)
S._cuentas.clear(); S._cuentas["cuenta_2"] = {"access_token": "AT", "user_id": "502960739", "nickname": "E"}
vaciar()
_sleep_real = time.sleep
time.sleep = lambda s: _sleep_real(0.01)         # el webhook espera 2 s antes de consultar a ML
import time as _t
for oid in ["903", "901", "904", "902", "901", "903"]:
    S._procesar_notificacion_orden(oid, "502960739")
_t.sleep(0.3)
time.sleep = _sleep_real
p = list(S._pedidos_ml.values())[0] if len(S._pedidos_ml) == 1 else None
esperado = sum(1 + o % 3 for o in range(901, 905))
check("4 órdenes (con avisos repetidos) → 1 pedido con todas las unidades", p is not None and uds(p) == esperado and len(p["_orders_agrupadas"]) == 4,
      (len(S._pedidos_ml), p and uds(p), esperado))
check("el pedido conserva fecha_cierre_ts (corte Flex 18:00)", p and p.get("fecha_cierre_ts") == "2026-10-14T10:01:00.000-04:00")

# ── 4. aviso de envío (shipments) con la orden fusionada dentro de otro pedido ──
print("4) webhook de envíos")
S.requests = types.SimpleNamespace(get=lambda url, **k: R({"status": "shipped", "order_id": 902, "logistic_type": "self_service"}), post=None)
S._procesar_notificacion_shipment("555", "502960739")
p = list(S._pedidos_ml.values())[0]
check("el estado del envío llega al pedido aunque la orden del aviso esté fusionada", p["estado_envio"] == "shipped" and p["logistica"] == "self_service", (p["estado_envio"], p["logistica"]))

# ── 5. control de unidades contra ML (solo informativo) ─────────────────────────
print("5) control de unidades del envío")
ped = {"10": orden(10, "S10", [it("A", 2), it("B", 1)], _orders_agrupadas=["10", "11"]),
       "20": orden(20, "S20", [it("C", 1)])}
def ml_stub(ruta, cuenta, params=None):
    if ruta.startswith("/shipments/S10"):
        return R({"status": "ready_to_ship", "substatus": "ready_to_print", "logistic_type": "self_service",
                  "shipping_items": [{"quantity": 2}, {"quantity": 1}, {"quantity": 13}]})
    if ruta.startswith("/shipments/S20"):
        return R({"status": "ready_to_ship", "substatus": "ready_to_print", "logistic_type": "cross_docking",
                  "shipping_items": [{"quantity": 1}]})
    return R([])
orig = S._ml_get_cuenta; S._ml_get_cuenta = ml_stub; cap.lineas.clear()
S._enriquecer_skus_cuenta(ped, "cuenta_2")
S._ml_get_cuenta = orig
check("unidades_envio se guarda", ped["10"].get("unidades_envio") == 16 and ped["20"].get("unidades_envio") == 1)
check("envío que NO coincide deja [PACK-CHECK] en el log", any("[PACK-CHECK]" in l and "S10" in l and "16" in l for l in cap.lineas), cap.lineas[-3:])
check("envío que coincide no ensucia el log", not any("[PACK-CHECK]" in l and "S20" in l for l in cap.lineas))
check("el control no cambia el pedido", uds(ped["10"]) == 3)

# ── 6. órdenes reales de producción ─────────────────────────────────────────────
print("6) foto real de producción (hoy, con envíos sueltos)")
snaps = [os.path.join(S_DIR, "snap", n) for n in ("pedidos_live_0814.json", "pedidos_live.json")]
for ruta in snaps:
    if not os.path.exists(ruta):
        continue
    nombre = os.path.basename(ruta)
    reales = [p for p in json.load(open(ruta, encoding="utf-8"))["pedidos"] if p.get("shipping_id")]
    for p in reales:
        p.pop("_orders_agrupadas", None)          # como si todos vinieran sueltos
    por_envio = {}
    for p in reales: por_envio.setdefault(p["shipping_id"], []).append(p)
    unidades = {k: sum(uds(x) for x in v) for k, v in por_envio.items()}
    g = S._agrupar_por_envio({p["order_id"]: copy.deepcopy(p) for p in reales})
    check(f"[{nombre}] {len(reales)} órdenes → {len(por_envio)} envíos (1 entrada por envío)", len(g) == len(por_envio), (len(g), len(por_envio)))
    check(f"[{nombre}] ningún envío pierde unidades al agrupar", all(uds(p) == unidades[p["shipping_id"]] for p in g.values()))
    multi = {k: v for k, v in por_envio.items() if len(v) > 1}
    for _ in range(5):
        vaciar()
        cola = [copy.deepcopy(p) for p in reales] * 2
        random.shuffle(cola)
        for p in cola:
            S._registrar_orden_webhook(p["_cuenta"], p)
        envios = [p["shipping_id"] for p in S._pedidos_ml.values()]
        if len(envios) != len(set(envios)) or any(uds(p) != unidades[p["shipping_id"]] for p in S._pedidos_ml.values()):
            break
    check(f"[{nombre}] por webhook, en desorden y duplicado: igual 1 entrada por envío con las unidades correctas ({len(multi)} envíos con varias órdenes)",
          len(envios) == len(set(envios)) and all(uds(p) == unidades[p["shipping_id"]] for p in S._pedidos_ml.values()),
          [(p["shipping_id"], uds(p), unidades[p["shipping_id"]]) for p in S._pedidos_ml.values() if uds(p) != unidades[p["shipping_id"]]][:3])

# ── 7. el agrupado nuevo da lo mismo que el viejo (sin items sin SKU) ──────────────
print("7) equivalencia con el agrupado anterior")
def viejo(pedidos):
    pa, vistos = {}, {}
    for oid, ped in pedidos.items():
        ship = ped.get("shipping_id", "")
        if not ship: pa[oid] = ped
        elif ship not in vistos: vistos[ship] = oid; pa[oid] = ped
        else:
            pp = pa[vistos[ship]]
            ck = lambda i: (i["sku"], i.get("color", ""), i.get("talle", ""))
            ex = {ck(i): i for i in pp["items"]}
            for i in ped.get("items", []):
                if ck(i) in ex: ex[ck(i)]["cantidad"] += i["cantidad"]
                else: pp["items"].append(i); ex[ck(i)] = i
            pp["total"] = pp.get("total", 0) + ped.get("total", 0)
            pp.setdefault("_orders_agrupadas", [vistos[ship]]).append(oid)
    return pa
for ruta in snaps:
    if os.path.exists(ruta):
        base = {p["order_id"]: p for p in json.load(open(ruta, encoding="utf-8"))["pedidos"]}
        for p in base.values(): p.pop("_orders_agrupadas", None)
        a, b = viejo(copy.deepcopy(base)), S._agrupar_por_envio(copy.deepcopy(base))
        check(f"[{os.path.basename(ruta)}] mismas entradas, items, total y órdenes agrupadas que el algoritmo anterior",
              sorted(a) == sorted(b) and all(skus(a[k]) == skus(b[k]) and a[k].get("total") == b[k].get("total")
                                              and a[k].get("_orders_agrupadas") == b[k].get("_orders_agrupadas") for k in a))

print(f"\nRESULTADO pack-por-envío (servidor): {ok} ok, {fa} fallas")
sys.exit(1 if fa else 0)
