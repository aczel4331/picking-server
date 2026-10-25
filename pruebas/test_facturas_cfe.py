"""Pruebas del lector de CFE y del emparejador factura ↔ venta (facturas_cfe.py). Sin red ni servidor."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import facturas_cfe as F  # noqa: E402

FALLOS = []


def ok(cond, msg):
    print(("  OK  " if cond else "  FALLA ") + msg)
    if not cond:
        FALLOS.append(msg)


def pdf_cfe(lineas):
    """PDF con texto real (como el de un facturador) armado con reportlab."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for ln in lineas:
        c.drawString(40, y, ln)
        y -= 16
    c.save()
    return buf.getvalue()


def factura(nombre, total, fecha="15/10/2026", serie="A", numero="1234", extra=()):
    return pdf_cfe(["EVEREST SAS   RUT EMISOR 214455660011", "e-Ticket",
                    f"Serie: {serie}   Nro: {numero}", f"Fecha: {fecha}",
                    f"Nombre: {nombre}", "Documento: 4.123.456-7", *extra,
                    "Descripcion            Cant   Precio",
                    "Remera algodon talle M   1   1.200,00",
                    "Subtotal  1.311,48", "IVA 22%  288,52",
                    f"TOTAL A PAGAR  $ {total}"])


def pack(pid, total, nombre, apellido, fecha="2026-10-10T12:00:00.000-03:00", buyer="b1", pagado=None):
    return {"pack_id": pid, "cuenta": "cuenta_2", "order_ids": [pid], "fecha": fecha,
            "total": total, "pagado": pagado if pagado is not None else total, "moneda": "UYU",
            "buyer_id": buyer, "nickname": "NICK" + buyer,
            "nombres": [{"nombre": nombre, "apellido": apellido}]}


print("== parse_monto")
for txt, esp in [("1.590,00", 1590.0), ("1,590.00", 1590.0), ("$ 1590", 1590.0), ("15,90", 15.9),
                 ("1.590", 1590.0), ("12.345.678,90", 12345678.9), ("abc", None)]:
    ok(F.parse_monto(txt) == esp, f"parse_monto({txt!r}) = {F.parse_monto(txt)} (esperado {esp})")

print("== leer_cfe")
f = F.leer_cfe(factura("José Pérez", "1.600,00"))
ok(f.get("total") == 1600.0, f"total leído = {f.get('total')}")
ok(F.tokens_nombre(f.get("nombre")) == {"jose", "perez"}, f"nombre leído = {f.get('nombre')!r}")
ok(f.get("fecha") == "2026-10-15", f"fecha leída = {f.get('fecha')}")
ok(f.get("clave") == "e-ticket:A-1234", f"clave = {f.get('clave')}")
ok(f.get("tipo") == "e-ticket", f"tipo = {f.get('tipo')}")
ok(F.leer_cfe(pdf_cfe(["NOTA DE CREDITO e-Ticket", "TOTAL 100,00"]))["tipo"] == "nota_credito", "nota de crédito detectada")

print("== emparejar")
# comprador con 3 compras de montos distintos en el mes
packs = [pack("P1", 1600.0, "Jose", "Perez"), pack("P2", 990.0, "Jose", "Perez", fecha="2026-10-03T10:00:00-03:00"),
         pack("P3", 2500.0, "Jose", "Perez", fecha="2026-10-12T10:00:00-03:00"),
         pack("P4", 1600.0, "Ana", "Gomez", buyer="b2")]
for total, esperado in [("1.600,00", "P1"), ("990,00", "P2"), ("2.500,00", "P3")]:
    p, motivo, _ = F.elegir_pack(F.leer_cfe(factura("José Pérez", total)), packs)
    ok(p is not None and p["pack_id"] == esperado, f"José Pérez ${total} → {p and p['pack_id']} ({motivo})")

# orden invertido + segundo apellido
p, _, _ = F.elegir_pack(F.leer_cfe(factura("PEREZ GOMEZ, JOSE LUIS", "1.600,00")), packs)
ok(p is not None and p["pack_id"] == "P1", "nombre invertido con 2do apellido → P1")

# monto coincide (P4 Ana) pero el nombre no → duda, sin elegir
p, motivo, cands = F.elegir_pack(F.leer_cfe(factura("Marta Silva", "1.600,00")), packs)
ok(p is None and cands and cands[0]["pack_id"] in ("P1", "P4"), f"nombre distinto → duda ({motivo})")

# nombre coincide pero monto no → duda con candidatas del comprador
p, motivo, cands = F.elegir_pack(F.leer_cfe(factura("José Pérez", "1.234,00")), packs)
ok(p is None and {c["pack_id"] for c in cands} == {"P1", "P2", "P3"}, f"monto distinto → duda ({motivo})")

# dos compras iguales del mismo comprador: dos facturas → dos packs distintos
iguales = [pack("Q1", 500.0, "Luis", "Rodriguez", fecha="2026-10-01T10:00:00-03:00"),
           pack("Q2", 500.0, "Luis", "Rodriguez", fecha="2026-10-08T10:00:00-03:00")]
f1 = F.leer_cfe(factura("Luis Rodríguez", "500,00", numero="1"))
p1, _, _ = F.elegir_pack(f1, iguales)
p2, _, _ = F.elegir_pack(F.leer_cfe(factura("Luis Rodríguez", "500,00", numero="2")), iguales, ocupados={p1["pack_id"]})
ok(p1 and p2 and p1["pack_id"] != p2["pack_id"], f"montos iguales → {p1 and p1['pack_id']} y {p2 and p2['pack_id']}")
p3, motivo, _ = F.elegir_pack(F.leer_cfe(factura("Luis Rodríguez", "500,00", numero="3")), iguales, ocupados={"Q1", "Q2"})
ok(p3 is None, f"tercera factura igual sin venta libre → duda ({motivo})")

# venta posterior a la factura no cuenta
p, _, _ = F.elegir_pack(F.leer_cfe(factura("José Pérez", "2.500,00", fecha="05/10/2026")), packs)
ok(p is None, "venta posterior a la factura no se elige")

# homónimos con mismo monto (compradores distintos) → duda
homo = [pack("H1", 700.0, "Juan", "Silva", buyer="x1"), pack("H2", 700.0, "Juan", "Silva", buyer="x2")]
p, motivo, cands = F.elegir_pack(F.leer_cfe(factura("Juan Silva", "700,00")), homo)
ok(p is None and len(cands) == 2, f"homónimos → duda ({motivo})")

# monto con envío (paid_amount) también vale
p, _, _ = F.elegir_pack(F.leer_cfe(factura("Ana Gomez", "1.750,00")), [pack("E1", 1600.0, "Ana", "Gomez", pagado=1750.0)])
ok(p is not None, "monto = pagado (con envío) → se acepta")

# empresa (razón social sin apellido)
emp = [{**pack("R1", 3000.0, "Ferreteria El Tornillo SRL", ""), "nombres": [{"nombre": "Ferreteria El Tornillo SRL", "apellido": ""}]}]
p, _, _ = F.elegir_pack(F.leer_cfe(factura("FERRETERIA EL TORNILLO S.R.L.", "3.000,00")), emp)
ok(p is not None, "razón social → R1")

# el nombre del receptor no tiene rótulo reconocible: se busca en el texto completo
sin_rot = pdf_cfe(["e-Ticket Serie B Nro 77", "Fecha 15/10/2026", "Maria Fernanda Lopez", "TOTAL 1.000,00"])
p, _, _ = F.elegir_pack(F.leer_cfe(sin_rot), [pack("T1", 1000.0, "Maria", "Lopez")])
ok(p is not None, "nombre sin rótulo encontrado en el texto")

print("== formato tabla (rótulos en una fila, valores abajo) — como el facturador de la DGI")
tabla = "\n".join([
    "MONOTRIBUTO SOCIAL MIDES", "Mi Comercio", "Juan Emisor Perez Rodriguez", "Calle 123 - Montevideo",
    " RUT EMISOR TIPO DE DOCUMENTO", "210000000018 e-Factura",
    "SERIE NUMERO FORMA DE PAGO", "A 2012 CONTADO",
    "RUT COMPRADOR NOMBRE", "210000000016 FERRETERIA EL TORNILLO SRL",
    "DOMICILIO CIUDAD DEPARTAMENTO", "AVENIDA ITALIA 2505MONTEVIDEO MONTEVIDEO",
    "FECHA DE DOCUMENTO MONEDA", "13/10/2026 UYU",
    "Descripción P. UnitarioCantidadImporte", "Servicio TE (0%)20.920 120.920,00",
    "Subtotal no gravado: $ 20920",
    "Subtotal: $ 20.920,00 Total IVA: $ 0,00 Total: $ 20.920,00",
    "Nro de CAE 90000000000 Fecha de vencimiento CAE: 19/05/2028", "CAE serie  A2001 al 4000"])
ft = F.leer_cfe_texto(tabla)
ok(ft["serie"] == "A" and ft["numero"] == "2012", f"serie/número = {ft['serie']}-{ft['numero']}")
ok(F.tokens_nombre(ft["nombre"]) == {"ferreteria", "tornillo"}, f"nombre = {ft['nombre']!r}")
ok(ft["total"] == 20920.0, f"total = {ft['total']} (no el importe pegado a la cantidad)")
ok(ft["fecha"] == "2026-10-13", f"fecha = {ft['fecha']} (no la del vencimiento del CAE)")
ok(ft["tipo"] == "e-factura", f"tipo = {ft['tipo']}")
venta_emp = {**pack("Z1", 20920.0, "Ferreteria El Tornillo S.R.L.", ""), "nombres": [{"nombre": "Ferreteria El Tornillo S.R.L.", "apellido": ""}]}
ok(F.elegir_pack(ft, [venta_emp])[0] is not None, "empresa del formato tabla → se sube")
ok(F.elegir_pack(ft, [pack("Z2", 20920.0, "Juan", "Perez")])[0] is None,
   "el nombre del EMISOR (también está en el PDF) no hace coincidir a un comprador")

print("== agrupar_packs")
ords = [{"id": 11, "pack_id": 900, "total_amount": 100, "paid_amount": 120, "date_created": "2026-10-02T10:00:00-03:00",
         "currency_id": "UYU", "buyer": {"id": 5, "first_name": "Ana", "last_name": "Gomez"}},
        {"id": 12, "pack_id": 900, "total_amount": 50, "date_created": "2026-10-02T10:00:01-03:00",
         "currency_id": "UYU", "buyer": {"id": 5}},
        {"id": 13, "pack_id": None, "total_amount": 70, "date_created": "2026-10-03T10:00:00-03:00", "buyer": {"id": 6}},
        {"id": 14, "status": "cancelled", "total_amount": 70, "buyer": {"id": 6}}]
ps = {p["pack_id"]: p for p in F.agrupar_packs(ords, "cuenta_2")}
ok(set(ps) == {"900", "13"}, f"packs = {sorted(ps)} (pack sin pack_id usa order_id, cancelada fuera)")
ok(ps["900"]["total"] == 150 and ps["900"]["pagado"] == 170, f"pack 900 total/pagado = {ps['900']['total']}/{ps['900']['pagado']}")

print()
print("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLAS")
sys.exit(1 if FALLOS else 0)
