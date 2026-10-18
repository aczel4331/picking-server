"""Chequeos de la guarda anti-respuesta-vieja (lote_id/rev) en los clientes moviles.
Uso: python pruebas/test_movil_sync.py
"""
import os, re, shutil, subprocess, sys, tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGINAS = [os.path.join(RAIZ, "templates", "movil.html"),
           os.path.join(RAIZ, "movil_static", "index.html")]


def leer(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def extraer_guarda(src):
    m = re.search(r"let _loteId = null, _rev = -1, _staleN = 0;.*?function _revReiniciar\(\)[^\n]*\n", src, re.S)
    assert m, "no se encontro la guarda _revEstado/_revAplicar/_revDescartar"
    return m.group(0)


def main():
    fallos = 0
    node = shutil.which("node")
    for p in PAGINAS:
        src = leer(p)
        n = os.path.relpath(p, RAIZ)
        guarda = extraer_guarda(src)
        checks = {
            "usa _revDescartar en las lecturas de /api/estado": src.count("_revDescartar(") >= 3,
            "usa _revEstado en el escaneo": src.count("_revEstado(") >= 2,
            "aplica rev (_revAplicar) tras aceptar": src.count("_revAplicar(") >= 3,
            "el escaneo desordenado no retrocede el contador": "Math.max(Number(" in src,
        }
        for k, ok in checks.items():
            print(("OK   " if ok else "FAIL ") + n + ": " + k)
            fallos += not ok
        if node:
            js = guarda + """
const a = (o) => { const e = _revEstado(o); _revAplicar(o); return e; };
const log = [];
log.push(a({lote_id:'A', rev:5}));   // nuevo
log.push(a({lote_id:'A', rev:6}));   // ok
log.push(a({lote_id:'A', rev:4}));   // stale
log.push(a({lote_id:'A', rev:6}));   // ok (igual)
log.push(a({lote_id:'B', rev:1}));   // nuevo lote, rev menor aceptado
log.push(a({lote_id:'B', rev:0}));   // stale
log.push(a({}));                     // servidor viejo: ok
console.log(log.join(','));
// recuperacion: el servidor retrocede (reinicio sin restaurar el lote) -> a la 3a respuesta vieja seguida se acepta
_revReiniciar();
const d = (o) => { const x = _revDescartar(o); if (!x) _revAplicar(o); return x ? 'descarta' : 'acepta'; };
const rec = [];
rec.push(d({lote_id:'L', rev:1000}));  // acepta (primera vez)
rec.push(d({lote_id:'L', rev:3}));     // descarta (1)
rec.push(d({lote_id:'L', rev:4}));     // descarta (2)
rec.push(d({lote_id:'L', rev:5}));     // acepta (3a): el servidor retrocedio de verdad
rec.push(d({lote_id:'L', rev:6}));     // acepta: ya sigue el rev nuevo
rec.push(d({lote_id:'L', rev:2}));     // descarta (respuesta vieja normal)
rec.push(d({lote_id:'L', rev:7}));     // acepta y reinicia el contador
console.log(rec.join(','));
// cerrar sesion / cambiar de colector: la guarda empieza limpia
_revReiniciar();
console.log(_revDescartar({lote_id:'X', rev:1}) ? 'descarta' : 'acepta');
"""
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as t:
                t.write(js)
            try:
                out = subprocess.run([node, t.name], capture_output=True, text=True).stdout.strip()
            finally:
                os.unlink(t.name)
            esperado = ("nuevo,ok,stale,ok,nuevo,stale,ok\n"
                        "acepta,descarta,descarta,acepta,acepta,descarta,acepta\nacepta")
            ok = out.replace("\r", "") == esperado
            print(("OK   " if ok else "FAIL ") + n + ": logica rev (" + out + ")")
            fallos += not ok
        else:
            print("SKIP logica JS (node no disponible)")
    print("RESULTADO:", "FALLO" if fallos else "OK")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
