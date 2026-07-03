"""
logibot_dashboard.py
====================
Dashboard de métricas para Logibot Picking Pro.

Se abre como ventana independiente (Toplevel) sin afectar la app principal.
Guarda métricas en metrics.json junto al .exe/.py.

Métricas registradas automáticamente:
  - Tiempo de colecta por lote (inicio → Fase 2)
  - SKUs procesados por lote
  - Canal (Flex vs Colecta)
  - Fecha y hora
  - Productos más colectados

Uso desde app_deposito.py:
    from logibot_dashboard import (registrar_inicio_lote, registrar_fin_lote,
                                    abrir_dashboard)
"""

import json
import os
import time
import datetime
import tkinter as tk
from tkinter import ttk

# ── Ruta del archivo de métricas ──────────────────────────────────────────────
METRICS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "metrics.json")

# ── Estado en memoria ─────────────────────────────────────────────────────────
_lote_inicio  = {}   # { canal: timestamp_inicio }
_lote_skus    = {}   # { canal: {sku: qty} }
_lote_pedidos = {}   # { canal: n_pedidos }


# ─────────────────────────────────────────────────────────────────────────────
# API pública — registrar eventos
# ─────────────────────────────────────────────────────────────────────────────

def registrar_inicio_lote(canal: str, n_pedidos: int, skus: dict):
    """Llamar cuando el lote se sube a Railway y empieza la colecta."""
    _lote_inicio[canal]  = time.time()
    _lote_skus[canal]    = dict(skus)
    _lote_pedidos[canal] = n_pedidos
    print(f"[METRICS] Inicio lote canal='{canal}' "
          f"pedidos={n_pedidos} skus={len(skus)}")


def registrar_fin_lote(canal: str):
    """Llamar cuando la colecta termina (transición a Fase 2)."""
    inicio = _lote_inicio.pop(canal, None)
    if inicio is None:
        return
    duracion_seg = int(time.time() - inicio)
    skus    = _lote_skus.pop(canal, {})
    n_peds  = _lote_pedidos.pop(canal, 0)

    entrada = {
        "ts":            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "canal":         canal,
        "duracion_seg":  duracion_seg,
        "n_pedidos":     n_peds,
        "n_skus":        len(skus),
        "total_uds":     sum(skus.values()),
        "skus":          skus,
    }

    _guardar_metrica(entrada)
    print(f"[METRICS] Fin lote canal='{canal}' "
          f"duración={_fmt_duracion(duracion_seg)} pedidos={n_peds}")


def _guardar_metrica(entrada: dict):
    """Agrega una entrada al archivo de métricas."""
    data = _cargar_metricas()
    data.append(entrada)
    # Conservar solo los últimos 500 lotes
    if len(data) > 500:
        data = data[-500:]
    try:
        with open(METRICS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[METRICS] Error guardando: {e}")


def _cargar_metricas() -> list:
    if not os.path.exists(METRICS_PATH):
        return []
    try:
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _fmt_duracion(seg: int) -> str:
    if seg < 60:
        return f"{seg}s"
    m, s = divmod(seg, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard UI
# ─────────────────────────────────────────────────────────────────────────────

def abrir_dashboard(parent):
    """Abre el dashboard de métricas como ventana independiente."""
    win = tk.Toplevel(parent)
    win.title("📊  Dashboard de Métricas — Logibot")
    win.geometry("1000x680")
    win.configure(bg="#0F172A")
    win.resizable(True, True)

    data = _cargar_metricas()

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = tk.Frame(win, bg="#1E293B", height=56)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    tk.Label(hdr, text="📊  DASHBOARD DE MÉTRICAS",
             bg="#1E293B", fg="white",
             font=("Segoe UI Black", 14)).pack(side="left", padx=20, pady=14)
    tk.Label(hdr, text=f"{len(data)} lotes registrados",
             bg="#1E293B", fg="#94A3B8",
             font=("Segoe UI", 9)).pack(side="right", padx=20)

    # ── Tabs ──────────────────────────────────────────────────────────────────
    nb = ttk.Notebook(win)
    nb.pack(fill="both", expand=True, padx=0, pady=0)

    _tab_resumen(nb, data)
    _tab_flex_vs_colecta(nb, data)
    _tab_productos(nb, data)
    _tab_historial(nb, data)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 1: Resumen General
# ─────────────────────────────────────────────────────────────────────────────

def _tab_resumen(nb, data):
    frame = tk.Frame(nb, bg="#0F172A")
    nb.add(frame, text="  📈 Resumen  ")

    if not data:
        tk.Label(frame, text="Sin datos aún.\nGenerá algunos lotes para ver métricas.",
                 bg="#0F172A", fg="#64748B",
                 font=("Segoe UI", 12)).pack(expand=True)
        return

    # ── KPIs ──────────────────────────────────────────────────────────────────
    total_lotes   = len(data)
    total_pedidos = sum(d.get("n_pedidos", 0) for d in data)
    total_uds     = sum(d.get("total_uds", 0) for d in data)
    durs          = [d["duracion_seg"] for d in data if d.get("duracion_seg", 0) > 0]
    prom_dur      = int(sum(durs) / len(durs)) if durs else 0
    mejor_dur     = min(durs) if durs else 0
    peor_dur      = max(durs) if durs else 0

    # Hoy
    hoy = datetime.date.today().strftime("%Y-%m-%d")
    data_hoy = [d for d in data if d.get("ts", "").startswith(hoy)]
    lotes_hoy = len(data_hoy)
    peds_hoy  = sum(d.get("n_pedidos", 0) for d in data_hoy)

    kpis = [
        ("🗂  Total lotes",         str(total_lotes),              "#3B82F6"),
        ("📦  Total pedidos",        f"{total_pedidos:,}",          "#8B5CF6"),
        ("📫  Unidades procesadas",  f"{total_uds:,}",              "#10B981"),
        ("⏱  Tiempo prom. colecta", _fmt_duracion(prom_dur),       "#F59E0B"),
        ("🏆  Mejor tiempo",         _fmt_duracion(mejor_dur),      "#06B6D4"),
        ("📅  Lotes hoy",            f"{lotes_hoy} ({peds_hoy} ped)", "#EC4899"),
    ]

    kpi_frame = tk.Frame(frame, bg="#0F172A")
    kpi_frame.pack(fill="x", padx=20, pady=20)

    for i, (lbl, val, color) in enumerate(kpis):
        card = tk.Frame(kpi_frame, bg="#1E293B",
                        relief="flat", bd=0, padx=18, pady=14)
        card.grid(row=i//3, column=i%3, padx=8, pady=8, sticky="ew")
        kpi_frame.columnconfigure(i%3, weight=1)
        tk.Label(card, text=val,
                 bg="#1E293B", fg=color,
                 font=("Segoe UI Black", 22)).pack(anchor="w")
        tk.Label(card, text=lbl,
                 bg="#1E293B", fg="#94A3B8",
                 font=("Segoe UI", 9)).pack(anchor="w")

    # ── Últimos 10 lotes ──────────────────────────────────────────────────────
    tk.Label(frame, text="Últimos lotes",
             bg="#0F172A", fg="#CBD5E1",
             font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=24, pady=(8,4))

    cols = ("Fecha", "Canal", "Pedidos", "SKUs", "Unid.", "Duración")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=10)
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=130, anchor="center")
    tree.column("Fecha", width=140)

    for d in reversed(data[-10:]):
        tree.insert("", "end", values=(
            d.get("ts", ""),
            d.get("canal", "").upper(),
            d.get("n_pedidos", 0),
            d.get("n_skus", 0),
            d.get("total_uds", 0),
            _fmt_duracion(d.get("duracion_seg", 0)),
        ))

    tree.pack(fill="both", expand=True, padx=16, pady=(0,16))


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2: Flex vs Colecta
# ─────────────────────────────────────────────────────────────────────────────

def _tab_flex_vs_colecta(nb, data):
    frame = tk.Frame(nb, bg="#0F172A")
    nb.add(frame, text="  ⚡ Flex vs 🚚 Colecta  ")

    if not data:
        tk.Label(frame, text="Sin datos.", bg="#0F172A", fg="#64748B",
                 font=("Segoe UI", 12)).pack(expand=True)
        return

    flex    = [d for d in data if d.get("canal") == "flex"]
    colecta = [d for d in data if d.get("canal") == "colecta"]
    otros   = [d for d in data if d.get("canal") not in ("flex","colecta")]

    def _stats(lotes):
        if not lotes:
            return {"lotes": 0, "pedidos": 0, "uds": 0, "prom": 0}
        durs = [d["duracion_seg"] for d in lotes if d.get("duracion_seg",0)>0]
        return {
            "lotes":   len(lotes),
            "pedidos": sum(d.get("n_pedidos",0) for d in lotes),
            "uds":     sum(d.get("total_uds",0) for d in lotes),
            "prom":    int(sum(durs)/len(durs)) if durs else 0,
        }

    sf = _stats(flex)
    sc = _stats(colecta)
    so = _stats(otros)

    comp = tk.Frame(frame, bg="#0F172A")
    comp.pack(fill="x", padx=20, pady=20)

    for titulo, stats, color, bg in [
        ("⚡  FLEX",    sf, "#8B5CF6", "#2D1B69"),
        ("🚚  COLECTA", sc, "#3B82F6", "#1E3A5F"),
        ("📦  OTROS",   so, "#6B7280", "#1E2432"),
    ]:
        if stats["lotes"] == 0:
            continue
        card = tk.Frame(comp, bg=bg, relief="flat", bd=0, padx=20, pady=16)
        card.pack(side="left", expand=True, fill="both", padx=8)
        tk.Label(card, text=titulo, bg=bg, fg=color,
                 font=("Segoe UI Black", 14)).pack(anchor="w")
        for lbl, val in [
            ("Lotes procesados",  str(stats["lotes"])),
            ("Pedidos totales",   f"{stats['pedidos']:,}"),
            ("Unidades totales",  f"{stats['uds']:,}"),
            ("Tiempo promedio",   _fmt_duracion(stats["prom"])),
        ]:
            r = tk.Frame(card, bg=bg)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=lbl, bg=bg, fg="#94A3B8",
                     font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
            tk.Label(r, text=val, bg=bg, fg="white",
                     font=("Segoe UI Semibold", 11)).pack(side="left")

    # Tabla comparativa por semana
    tk.Label(frame, text="Comparativa últimos 7 días",
             bg="#0F172A", fg="#CBD5E1",
             font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=24, pady=(16,4))

    hoy   = datetime.date.today()
    filas = []
    for i in range(7):
        dia    = (hoy - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        dia_f  = (hoy - datetime.timedelta(days=i)).strftime("%d/%m")
        data_d = [d for d in data if d.get("ts","").startswith(dia)]
        f_lotes = len([d for d in data_d if d.get("canal")=="flex"])
        c_lotes = len([d for d in data_d if d.get("canal")=="colecta"])
        f_peds  = sum(d.get("n_pedidos",0) for d in data_d if d.get("canal")=="flex")
        c_peds  = sum(d.get("n_pedidos",0) for d in data_d if d.get("canal")=="colecta")
        filas.append((dia_f, f_lotes, f_peds, c_lotes, c_peds))

    cols = ("Día", "Lotes Flex", "Peds Flex", "Lotes Colecta", "Peds Colecta")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=7)
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=140, anchor="center")
    for f in filas:
        tree.insert("", "end", values=f)
    tree.pack(fill="x", padx=16, pady=(0,16))


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3: Productos más vendidos
# ─────────────────────────────────────────────────────────────────────────────

def _tab_productos(nb, data):
    frame = tk.Frame(nb, bg="#0F172A")
    nb.add(frame, text="  🏆 Top Productos  ")

    if not data:
        tk.Label(frame, text="Sin datos.", bg="#0F172A", fg="#64748B",
                 font=("Segoe UI", 12)).pack(expand=True)
        return

    # Contar SKUs en todos los lotes
    conteo = {}
    for lote in data:
        for sku, qty in lote.get("skus", {}).items():
            conteo[sku] = conteo.get(sku, 0) + qty

    top = sorted(conteo.items(), key=lambda x: x[1], reverse=True)[:50]

    tk.Label(frame, text="Top 50 SKUs más procesados (todos los tiempos)",
             bg="#0F172A", fg="#CBD5E1",
             font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=24, pady=(16,6))

    cols = ("Pos.", "SKU", "Unidades procesadas", "% del total")
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=20)
    tree.heading("Pos.",               text="#")
    tree.heading("SKU",                text="SKU")
    tree.heading("Unidades procesadas",text="Unidades")
    tree.heading("% del total",        text="% del total")
    tree.column("Pos.",                width=50,  anchor="center")
    tree.column("SKU",                 width=180, anchor="center")
    tree.column("Unidades procesadas", width=200, anchor="center")
    tree.column("% del total",         width=140, anchor="center")

    total_uds = sum(conteo.values()) or 1
    for i, (sku, qty) in enumerate(top, 1):
        pct = f"{qty/total_uds*100:.1f}%"
        tree.insert("", "end", values=(i, sku, f"{qty:,}", pct))

    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True, padx=(16,0), pady=(0,16))
    sb.pack(side="left", fill="y", pady=(0,16))


# ─────────────────────────────────────────────────────────────────────────────
# Tab 4: Historial completo
# ─────────────────────────────────────────────────────────────────────────────

def _tab_historial(nb, data):
    frame = tk.Frame(nb, bg="#0F172A")
    nb.add(frame, text="  📋 Historial  ")

    if not data:
        tk.Label(frame, text="Sin datos.", bg="#0F172A", fg="#64748B",
                 font=("Segoe UI", 12)).pack(expand=True)
        return

    cols = ("Fecha", "Canal", "Pedidos", "SKUs distintos", "Unidades", "Duración")
    tree = ttk.Treeview(frame, columns=cols, show="headings")
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=140, anchor="center")
    tree.column("Fecha", width=160)

    for d in reversed(data):
        tree.insert("", "end", values=(
            d.get("ts", ""),
            d.get("canal", "").upper(),
            d.get("n_pedidos", 0),
            d.get("n_skus", 0),
            d.get("total_uds", 0),
            _fmt_duracion(d.get("duracion_seg", 0)),
        ))

    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True, padx=(16,0), pady=16)
    sb.pack(side="left", fill="y", pady=16)
