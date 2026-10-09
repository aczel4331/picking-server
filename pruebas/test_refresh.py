"""Refresco automatico de pedidos: ya NO se pausa por lotes activos, cadencia alrededor del corte
Flex, y el webhook guarda la hora de cierre para que el corte 18:00 se aplique."""
import os, sys, time, threading, tempfile, types, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, PICKING_API_KEY="everest2024", APP_SECRET_KEY="s" * 40, ML_APP_ID="x", ML_SECRET_KEY="x")
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

print("cadencia")
UY = dt.timezone(dt.timedelta(hours=-3))
def a_las(h, m):
    S._hora_uruguay = lambda: dt.datetime(2026, 10, 8, h, m, tzinfo=UY)
    return S._intervalo_auto_refresh()
check("mañana (09:00): cada 3 min", a_las(9, 0) == 180)
check("tarde (16:59): cada 3 min", a_las(16, 59) == 180)
check("17:00-18:30 (corte Flex): cada 60 s", a_las(17, 0) == 60 and a_las(18, 5) == 60 and a_las(18, 29) == 60)
check("18:30-19:00: cada 5 min", a_las(18, 30) == 300 and a_las(18, 59) == 300)
S._hora_uruguay = lambda: dt.datetime(2026, 10, 8, 17, 30, tzinfo=UY)
check("si un refresco tarda 45 s, espera >= 90 s", S._intervalo_auto_refresh(45) == 90)

print("el refresco NO se pausa con lotes activos")
llamadas = []
S._refresh_pedidos_worker = lambda: llamadas.append(time.time())
S._cuentas["c"] = {"access_token": "x"}
S._pedidos_ml["1"] = {"order_id": "1"}
S._sync_pausado = lambda: True               # todos los lotes "en_lote": antes frenaba el refresco
S._en_modo_descanso = lambda: False
S._intervalo_auto_refresh = lambda d=0.0: 0.05
t = threading.Thread(target=S._auto_refresh_loop, daemon=True); t.start()
time.sleep(0.6)
check("con lotes activos igual refresca", len(llamadas) >= 3, len(llamadas))
n1 = len(llamadas)
S._en_modo_descanso = lambda: True
time.sleep(0.3); n_a = len(llamadas)
time.sleep(0.4)
check("en descanso (19:00-06:45) no refresca", len(llamadas) == n_a, (n_a, len(llamadas)))
S._pedidos_ml.clear()
time.sleep(0.4)
check("en descanso con memoria vacia, refresca igual (como antes)", len(llamadas) > n_a)

print("webhook: guarda la hora de cierre")
class R:
    status_code = 200
    def __init__(self, j): self._j = j
    def json(self): return self._j
orden = {"status": "paid", "id": 777, "date_created": "2026-10-08T18:05:01.000-04:00", "date_closed": "2026-10-08T18:07:45.000-04:00",
         "order_items": [{"item": {"id": "MLU1", "title": "X", "seller_custom_field": "SKU1"}, "quantity": 1, "unit_price": 10}],
         "shipping": {"id": 55}, "buyer": {"nickname": "B"}, "total_amount": 10, "currency_id": "UYU", "tags": []}
S.requests = types.SimpleNamespace(get=lambda *a, **k: R(orden), post=None)
S._cuentas.clear(); S._cuentas["cuenta_2"] = {"access_token": "AT", "user_id": "502960739", "nickname": "E"}
S._procesar_notificacion_orden("777", "502960739")
p = S._pedidos_ml.get("777")
check("el pedido del webhook trae fecha_cierre_ts y fecha_ts", p and p.get("fecha_cierre_ts") == orden["date_closed"] and p.get("fecha_ts") == orden["date_created"], p)

print("aviso de OTRA cuenta de ML: se ignora")
S._pedidos_ml.pop("777", None); llamadas_ml = []
S.requests = types.SimpleNamespace(get=lambda *a, **k: (llamadas_ml.append(a), R(orden))[1], post=None)
S._procesar_notificacion_orden("777", "3121869818")
S._procesar_notificacion_shipment("5", "3121869818")
check("user_id ajeno: no consulta a ML ni agrega pedidos", llamadas_ml == [] and "777" not in S._pedidos_ml, (llamadas_ml, list(S._pedidos_ml)))
S._procesar_notificacion_orden("777", "")
check("sin user_id y una sola tienda: sigue funcionando como antes", "777" in S._pedidos_ml)

print("la app oculta ese pedido (cerro 18:07 > corte 18:00) y muestra uno de 17:55")
sys.path.insert(0, REPO)
import app_deposito as A
class Fake(A.AsistenteDepositoApp):
    def __init__(self): pass
app = Fake(); app.config = {}; app._cortes = {"flex": {"week": 18, "saturday": 18, "sunday": 18}, "colecta": {}}
hoy = dt.datetime.now(UY).date()
def ped(hh, mm):
    return {"logistica": "self_service", "substatus": "ready_to_print", "estado_envio": "ready_to_ship",
            "fecha_cierre_ts": dt.datetime(hoy.year, hoy.month, hoy.day, hh, mm, 0, tzinfo=UY).isoformat()}
check("cierre 18:07 -> es de mañana (oculto)", app._es_pedido_impreso(ped(18, 7)) is True)
check("cierre 17:55 -> es de hoy (visible)", app._es_pedido_impreso(ped(17, 55)) is False)
print(f"\nRESULTADO refresco: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
