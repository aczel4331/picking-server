"""
Facturas CFE → ventas de Mercado Libre.

Funciones puras (sin Flask ni red) para:
  · leer un PDF de CFE uruguayo (e-Ticket / e-Factura) y sacar receptor, total, fecha, serie/número;
  · comparar una factura contra los packs (ventas) de la tienda por NOMBRE + MONTO;
  · elegir a qué pack va, o decir que hay duda.

Regla de decisión (la pidió el dueño): se sube sola SOLO si coinciden monto y nombre.
El número de documento no se usa (no hay cómo compararlo de forma confiable).
"""
import hashlib
import io
import re
import unicodedata
from datetime import datetime, timedelta

TOLERANCIA_MONTO = 1.0          # UYU de diferencia aceptados (redondeos del facturador)
TAMANO_MAX_PDF   = 1024 * 1024  # Mercado Libre acepta hasta 1 MB por factura

# Palabras que no ayudan a distinguir a una persona/empresa
_VACIAS = {"de", "del", "la", "las", "los", "el", "y", "e", "da", "do", "dos", "das", "san",
           "sa", "s", "a", "srl", "ltda", "sas", "sociedad", "anonima", "limitada", "cia", "uy"}


# ══════════════════════════════════════════════════════════════════════════════
# Normalización
# ══════════════════════════════════════════════════════════════════════════════

def _sin_tildes(txt):
    return "".join(c for c in unicodedata.normalize("NFKD", str(txt or ""))
                   if not unicodedata.combining(c))


def tokens_nombre(txt):
    """'José  PÉREZ-Gómez' → {'jose', 'perez', 'gomez'} (sin tildes, sin palabras vacías)."""
    t = re.sub(r"[^a-z0-9ñ]+", " ", _sin_tildes(txt).lower())
    return {w for w in t.split() if len(w) >= 2 and w not in _VACIAS and not w.isdigit()}


def parse_monto(txt):
    """'1.590,00' / '1,590.00' / '$ 1590' / '15,9' → float (None si no es un número)."""
    s = re.sub(r"[^0-9,.\-]", "", str(txt or ""))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):        # 1.590,00
            s = s.replace(".", "").replace(",", ".")
        else:                                   # 1,590.00
            s = s.replace(",", "")
    elif "," in s:
        ent, _, dec = s.rpartition(",")
        s = (ent.replace(",", "") + "." + dec) if len(dec) != 3 else s.replace(",", "")
    elif s.count(".") == 1:
        ent, _, dec = s.partition(".")
        if len(dec) == 3:                       # 1.590 → miles
            s = ent + dec
    else:
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Lectura del PDF
# ══════════════════════════════════════════════════════════════════════════════

_NUM = r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|-?\d+(?:[.,]\d{1,2})?"
_RE_FECHA = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")
# Rótulo de total con el valor en la línea de abajo (el valor en la misma línea lo resuelve _RE_TOTAL_LINEA)
_ROT_TOTAL = [r"total\s+a\s+pagar", r"monto\s+total", r"importe\s+total", r"total\s+general",
              r"total\s+\(?uyu\)?", r"total"]
# Líneas con "total" que no son el total de la factura
_RE_NO_ES_TOTAL = re.compile(r"sub\s*-?\s*total|total\s+iva|iva\s+total|total\s+neto|no\s+gravado|"
                             r"cantidad|items?\b|art[ií]culos", re.I)
_ROT_NOMBRE = [r"raz[oó]n\s+social(?:\s+receptor)?", r"nombre\s+(?:del\s+)?(?:receptor|cliente|comprador)",
               r"receptor", r"cliente", r"comprador", r"se[nñ]or(?:es|\(es\))?", r"sr\.?\s*/?\s*sra\.?",
               r"nombre"]
_NO_ES_NOMBRE = re.compile(r"\b(rut|r\.u\.t|ci|c\.i|documento|direcci[oó]n|domicilio|tel[eé]fono|"
                           r"fecha|moneda|consumidor\s+final|emisor)\b", re.I)
# Palabras de encabezado de tabla: si el "valor" son solo estas, el dato está en la fila de abajo
_ENCABEZADOS = {"nombre", "razon", "social", "comprador", "receptor", "cliente", "domicilio", "ciudad",
                "departamento", "direccion", "pais", "rut", "documento", "tipo", "serie", "numero",
                "forma", "pago", "fecha", "moneda"}
# "Total: $ 20.920,00" en la misma línea (no "Subtotal" ni "Total IVA")
_RE_TOTAL_LINEA = [re.compile(r"total\s+a\s+pagar\s*:?\s*(?:\$|UYU|U\$S|USD)?\s*(" + _NUM + r")", re.I),
                   re.compile(r"(?:monto|importe)\s+total\s*:?\s*(?:\$|UYU|U\$S|USD)?\s*(" + _NUM + r")", re.I),
                   re.compile(r"(?<![a-z])total(?!\s*(?:iva|neto|no\s+gravado))\s*:?\s*"
                              r"(?:\$|UYU|U\$S|USD)?\s*(" + _NUM + r")", re.I)]


MAX_PAGINAS = 5      # un CFE ocupa una o dos páginas: no se lee entero un PDF enorme


def texto_pdf(pdf_bytes):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in list(r.pages)[:MAX_PAGINAS])


def _lineas(texto):
    return [ln.strip() for ln in str(texto or "").splitlines() if ln.strip()]


def _valor_tras_rotulo(lineas, rotulos, validar):
    """Busca 'Rótulo: valor' en la misma línea o el valor en la línea siguiente."""
    for rot in rotulos:
        pat = re.compile(r"(?:^|\b)" + rot + r"\b\s*[:\-]?\s*(.*)$", re.I)
        for i, ln in enumerate(lineas):
            m = pat.search(_sin_tildes(ln))
            if not m:
                continue
            # recortar el valor sobre la línea original (con tildes) en la misma posición
            resto = ln[m.start(1):].strip() if m.group(1) else ""
            for cand in ([resto] if resto else []) + lineas[i + 1:i + 2]:
                v = validar(cand)
                if v:
                    return v
    return None


def _nombre_valido(txt):
    txt = re.sub(r"^\s*[\d.\-/ ]{6,}\s*", "", str(txt or ""))      # "218185660016 POCITOS COLOR SRL"
    txt = re.split(r"\s{2,}|\t|\b(?:RUT|R\.U\.T|C\.?I\.?|Documento)\b", txt, maxsplit=1)[0]
    txt = txt.strip(" :-|")
    toks = tokens_nombre(txt)
    if not txt or _NO_ES_NOMBRE.search(_sin_tildes(txt)) or not toks or toks <= _ENCABEZADOS:
        return None
    if sum(c.isdigit() for c in txt) > 3:
        return None
    return txt[:120]


def _total_en_linea(lineas):
    """'Subtotal: $ 20.920,00 Total IVA: $ 0,00 Total: $ 20.920,00' → 20920 (el último 'Total' de verdad)."""
    for pat in _RE_TOTAL_LINEA:
        hallados = [parse_monto(m.group(1)) for ln in lineas for m in pat.finditer(_sin_tildes(ln))]
        hallados = [n for n in hallados if n is not None and n > 0]
        if hallados:
            return hallados[-1]
    return None


def _total_de_linea(txt):
    if _RE_NO_ES_TOTAL.search(_sin_tildes(txt)):
        return None
    nums = [parse_monto(n) for n in re.findall(_NUM, txt)]
    nums = [n for n in nums if n is not None and n > 0]
    return nums[-1] if nums else None


def _fecha(lineas):
    """Fecha del documento: la de la línea 'Fecha…' (o la de abajo si es un encabezado de tabla),
    nunca la del vencimiento del CAE."""
    venc = re.compile(r"venc", re.I)
    candidatas = []
    for i, ln in enumerate(lineas):
        if re.search(r"fecha", _sin_tildes(ln), re.I) and not venc.search(ln):
            candidatas += [ln] + lineas[i + 1:i + 2]
    candidatas += [ln for ln in lineas if not venc.search(ln)]
    for ln in candidatas:
        for m in _RE_FECHA.finditer(ln):
            try:
                if m.group(1):
                    return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
                return datetime(int(m.group(4)), int(m.group(5)), int(m.group(6))).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def leer_cfe_texto(texto):
    """Campos de una factura a partir de su texto. Los que no se encuentran quedan vacíos."""
    lineas = _lineas(texto)
    plano = _sin_tildes(texto)

    tipo = ""
    if re.search(r"nota\s+de\s+cr[eé]dito", plano, re.I):
        tipo = "nota_credito"
    elif re.search(r"e-?\s*factura", plano, re.I):
        tipo = "e-factura"
    elif re.search(r"e-?\s*ticket", plano, re.I):
        tipo = "e-ticket"

    serie = numero = ""
    m = re.search(r"\bserie\b\s*:?\s*([A-Z]{1,2})\b[\s\S]{0,40}?\b(?:n[º°o.]*|nro\.?|numero)\s*:?\s*(\d{1,10})",
                  plano, re.I)
    if not m:
        # encabezado de tabla "SERIE NUMERO ..." con los valores en la fila de abajo: "A 2012 CONTADO"
        m = re.search(r"\bserie\b[^\n]*\n\s*([A-Z]{1,2})\s+(\d{1,10})\b", plano, re.I)
    if m:
        serie, numero = m.group(1).upper(), m.group(2).lstrip("0") or "0"
    else:
        m = re.search(r"\be-?\s*(?:ticket|factura)\b[^\n]{0,30}?\b([A-Z]{1,2})\s*[-–]?\s*(\d{4,10})\b", plano, re.I)
        if m:
            serie, numero = m.group(1).upper(), m.group(2).lstrip("0") or "0"

    # sin rótulo de total no se adivina (el mayor importe podía ser una CI): queda None → duda
    total = _total_en_linea(lineas)
    if total is None:
        total = _valor_tras_rotulo(lineas, _ROT_TOTAL, _total_de_linea)

    moneda = "USD" if re.search(r"U\$S|US\$|\bUSD\b|d[oó]lares", plano, re.I) else "UYU"

    nombre = _valor_tras_rotulo(lineas, _ROT_NOMBRE, _nombre_valido) or ""
    return {
        "tipo": tipo,
        "serie": serie,
        "numero": numero,
        "nombre": nombre,
        "total": total,
        "moneda": moneda,
        "fecha": _fecha(lineas),
        # el texto completo solo se usa si no hay rótulo de receptor: así el nombre del emisor
        # (que también está en el PDF) nunca hace coincidir a un comprador
        "texto_tokens": [] if nombre else sorted(tokens_nombre(texto)),
    }


def leer_cfe(pdf_bytes):
    """Lee un PDF de CFE. Agrega 'clave' (tipo + serie-número, o hash) y 'sha256' para no subirla dos
    veces. El tipo va en la clave: un e-Ticket A-100 y una e-Factura A-100 son documentos distintos."""
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        texto = texto_pdf(pdf_bytes)
    except Exception as e:
        return {"error": f"No se pudo leer el PDF: {e}", "sha256": sha, "clave": "h:" + sha[:16]}
    f = leer_cfe_texto(texto)
    f["sha256"] = sha
    f["clave"] = (f"{f['tipo'] or 'cfe'}:{f['serie']}-{f['numero']}" if f["serie"] and f["numero"]
                  else "h:" + sha[:16])
    if not texto.strip():
        f["error"] = "El PDF no tiene texto (¿es una imagen escaneada?)"
    return f


# ══════════════════════════════════════════════════════════════════════════════
# Ventas (packs) y comparación
# ══════════════════════════════════════════════════════════════════════════════

def agrupar_packs(ordenes, cuenta_id=""):
    """Órdenes de /orders/search → packs. Un pack = una venta (pack_id, o el order_id si no tiene)."""
    packs, vistas = {}, set()
    for o in ordenes or []:
        oid = str(o.get("id") or "")
        # la paginación de ML puede repetir una orden: contarla dos veces cambiaría el monto
        if not oid or oid in vistas or str(o.get("status") or "paid") in ("cancelled", "invalid"):
            continue
        vistas.add(oid)
        pid = str(o.get("pack_id") or "") or oid
        b = o.get("buyer") or {}
        p = packs.setdefault(pid, {
            "pack_id": pid, "cuenta": cuenta_id, "order_ids": [], "fecha": "",
            "total": 0.0, "pagado": 0.0, "moneda": o.get("currency_id") or "UYU",
            "buyer_id": str(b.get("id") or ""), "nickname": b.get("nickname") or "",
            "nombres": [],
        })
        p["order_ids"].append(oid)
        p["total"] = round(p["total"] + float(o.get("total_amount") or 0), 2)
        p["pagado"] = round(p["pagado"] + float(o.get("paid_amount") or o.get("total_amount") or 0), 2)
        fc = str(o.get("date_created") or "")
        if fc and (not p["fecha"] or fc < p["fecha"]):
            p["fecha"] = fc
        nom = {"nombre": b.get("first_name") or "", "apellido": b.get("last_name") or ""}
        if (nom["nombre"] or nom["apellido"]) and nom not in p["nombres"]:
            p["nombres"].append(nom)
    return list(packs.values())


def monto_coincide(factura, pack, tolerancia=TOLERANCIA_MONTO):
    t = factura.get("total")
    if t is None:
        return False
    if factura.get("moneda") and pack.get("moneda") and factura["moneda"] != pack["moneda"]:
        return False
    return any(abs(float(v) - float(t)) <= tolerancia
               for v in (pack.get("total"), pack.get("pagado")) if v is not None)


def _coincide_persona(nombre_fact, texto_fact, nom):
    """nom = {'nombre': ..., 'apellido': ...}. Persona: hace falta nombre Y apellido.
    Empresa (sin apellido): todas las palabras de la razón social."""
    pila, ape = tokens_nombre(nom.get("nombre")), tokens_nombre(nom.get("apellido"))
    for es_rotulo, fuente in ((True, nombre_fact), (False, texto_fact)):
        if not fuente:
            continue
        if pila and ape:
            if (pila & fuente) and (ape & fuente):
                return True
        else:
            razon = pila or ape
            # una razón social de una sola palabra solo vale contra el rótulo del receptor
            if razon and razon <= fuente and (len(razon) >= 2 or es_rotulo):
                return True
    return False


def nombre_coincide(factura, pack):
    nombre_fact = tokens_nombre(factura.get("nombre"))
    texto_fact = set(factura.get("texto_tokens") or [])
    return any(_coincide_persona(nombre_fact, texto_fact, n) for n in pack.get("nombres") or [])


def _fecha_ok(factura, pack):
    """La venta no puede ser posterior a la factura (1 día de margen por zona horaria)."""
    if not factura.get("fecha") or not pack.get("fecha"):
        return True
    try:
        ff = datetime.strptime(factura["fecha"], "%Y-%m-%d")
        return pack["fecha"][:10] <= (ff + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return True


def resumen_pack(p, motivo=""):
    nom = next((f"{n.get('nombre','')} {n.get('apellido','')}".strip()
                for n in p.get("nombres") or [] if n.get("nombre") or n.get("apellido")), "")
    return {"pack_id": p["pack_id"], "cuenta": p.get("cuenta", ""), "fecha": (p.get("fecha") or "")[:10],
            "total": p.get("total"), "pagado": p.get("pagado"), "moneda": p.get("moneda"),
            "comprador": nom or p.get("nickname", ""), "nickname": p.get("nickname", ""),
            "order_ids": p.get("order_ids", []), "motivo": motivo}


def elegir_pack(factura, packs, ocupados=()):
    """
    Devuelve (pack | None, motivo, candidatas).
      pack != None  → seguro: coinciden monto y nombre (se puede subir sola).
      pack == None  → duda: candidatas trae las ventas posibles (la primera es la propuesta).
    'ocupados' = pack_ids que ya tienen factura (en ML o en el registro local).
    """
    ocupados = {str(x) for x in ocupados}
    libres = [p for p in packs if str(p["pack_id"]) not in ocupados and _fecha_ok(factura, p)]
    if factura.get("tipo") == "nota_credito":
        return None, "Es una nota de crédito: no se sube automáticamente", []
    if factura.get("total") is None:
        return None, "No se pudo leer el total de la factura", []

    con_monto = [p for p in libres if monto_coincide(factura, p)]
    seguros = [p for p in con_monto if nombre_coincide(factura, p)]

    def _cercania(p):          # la más reciente que no sea posterior a la factura va primero
        return p.get("fecha") or ""

    if seguros:
        compradores = {p.get("buyer_id") or p["pack_id"] for p in seguros}
        if len(compradores) == 1:
            elegido = sorted(seguros, key=_cercania, reverse=True)[0]
            motivo = "Monto y nombre coinciden"
            if len(seguros) > 1:
                motivo += f" (el comprador tiene {len(seguros)} compras de ese monto sin factura: va a la más reciente)"
            return elegido, motivo, [resumen_pack(p) for p in seguros]
        cands = [resumen_pack(p, "monto y nombre coinciden") for p in sorted(seguros, key=_cercania, reverse=True)]
        return None, "Hay compradores distintos con el mismo nombre y monto", cands

    solo_nombre = [p for p in libres if p not in con_monto and nombre_coincide(factura, p)]
    cands = ([resumen_pack(p, "el monto coincide, el nombre no") for p in sorted(con_monto, key=_cercania, reverse=True)]
             + [resumen_pack(p, "el nombre coincide, el monto no")
                for p in sorted(solo_nombre, key=lambda p: abs(float(p.get("total") or 0)
                                                                - float(factura["total"])))])
    if con_monto:
        motivo = "El monto coincide pero el nombre del comprador no"
    elif solo_nombre:
        motivo = "El comprador coincide pero el monto no"
    else:
        motivo = "No se encontró ninguna venta con ese monto ni ese comprador"
    return None, motivo, cands[:10]
