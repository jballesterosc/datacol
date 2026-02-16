"""
Build script for the Security Dashboard (Homicidios & Desaparecidos).

Loads incidencia delictiva and RNPDNO data, downloads CONAPO population,
computes per-capita rates, and generates a Chart.js dashboard at
docs/seguridad/index.html.

Usage:
    python build_seguridad.py
"""

import io
import json
import os

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INCIDENCIA_CSV = os.path.join(BASE_DIR, "_data", "incidencia_delictiva.csv")
RNPDNO_CSV = os.path.join(BASE_DIR, "_data", "rnpdno_desaparecidos.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "docs", "seguridad")
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")

CONAPO_URL = "https://repodatos.atdt.gob.mx/CONAPO/proyecciones/00_Pob_Inicio_1950_2070.csv"

DISPLAY_NAMES = {
    1: "Aguascalientes", 2: "Baja California", 3: "Baja California Sur",
    4: "Campeche", 5: "Coahuila", 6: "Colima", 7: "Chiapas",
    8: "Chihuahua", 9: "Ciudad de México", 10: "Durango",
    11: "Guanajuato", 12: "Guerrero", 13: "Hidalgo", 14: "Jalisco",
    15: "Estado de México", 16: "Michoacán", 17: "Morelos", 18: "Nayarit",
    19: "Nuevo León", 20: "Oaxaca", 21: "Puebla", 22: "Querétaro",
    23: "Quintana Roo", 24: "San Luis Potosí", 25: "Sinaloa", 26: "Sonora",
    27: "Tabasco", 28: "Tamaulipas", 29: "Tlaxcala", 30: "Veracruz",
    31: "Yucatán", 32: "Zacatecas",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_incidencia():
    df = pd.read_csv(INCIDENCIA_CSV)
    df = df[df["subtipo_delito"] == "Homicidio doloso"].copy()
    df["fecha"] = pd.to_datetime(df["fecha"])
    df = df[["fecha", "anio", "mes", "clave_ent", "incidencia_delictiva"]].copy()
    return df.rename(columns={"incidencia_delictiva": "count"})


def load_rnpdno():
    df = pd.read_csv(RNPDNO_CSV)
    df = df[df["categoria"] == "Personas desaparecidas y no localizadas"].copy()
    df["fecha"] = pd.to_datetime(df["fecha"])
    df = df[["fecha", "anio", "mes", "clave_ent", "valor"]].copy()
    return df.rename(columns={"valor": "count"})


def download_conapo():
    print("Downloading CONAPO population data...")
    resp = requests.get(CONAPO_URL, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    pob = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8")
    return (
        pob.groupby(["ANIO", "CVE_GEO"], as_index=False)["POBLACION"]
        .sum()
        .rename(columns={"ANIO": "anio", "CVE_GEO": "clave_ent", "POBLACION": "poblacion"})
    )


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_series(df, pob):
    """Merge population, compute tasa_100k, add Nacional aggregate."""
    df = df.merge(pob, on=["anio", "clave_ent"], how="left")
    df["tasa_100k"] = ((df["count"] / df["poblacion"]) * 100_000).round(4)

    # Nacional aggregate
    nac = (
        df.groupby(["fecha", "anio", "mes"], as_index=False)
        .agg({"count": "sum", "poblacion": "sum"})
    )
    nac["clave_ent"] = 0
    nac["tasa_100k"] = ((nac["count"] / nac["poblacion"]) * 100_000).round(4)

    df = pd.concat([df, nac], ignore_index=True)
    df = df.sort_values(["clave_ent", "fecha"])
    return df


def build_json_payload(df_h, df_d):
    """Build the JSON data structure consumed by the frontend."""
    names = {0: "Nacional", **DISPLAY_NAMES}
    states = ["Nacional"] + sorted(DISPLAY_NAMES.values())

    data = {}
    for clave, name in names.items():
        h = df_h[df_h["clave_ent"] == clave].sort_values("fecha")
        d = df_d[df_d["clave_ent"] == clave].sort_values("fecha")

        h_dates = set(h["fecha"].dt.strftime("%Y-%m-%d"))
        d_dates = set(d["fecha"].dt.strftime("%Y-%m-%d"))
        all_dates = sorted(h_dates | d_dates)

        h_map = dict(zip(h["fecha"].dt.strftime("%Y-%m-%d"), h["tasa_100k"]))
        d_map = dict(zip(d["fecha"].dt.strftime("%Y-%m-%d"), d["tasa_100k"]))
        h_count_map = dict(zip(h["fecha"].dt.strftime("%Y-%m-%d"), h["count"]))
        d_count_map = dict(zip(d["fecha"].dt.strftime("%Y-%m-%d"), d["count"]))

        data[name] = {
            "dates": all_dates,
            "homicidios": [round(h_map.get(dt, None) or 0, 2) if dt in h_map else None for dt in all_dates],
            "desaparecidos": [round(d_map.get(dt, None) or 0, 2) if dt in d_map else None for dt in all_dates],
            "homicidios_count": [int(h_count_map.get(dt, 0)) if dt in h_count_map else None for dt in all_dates],
            "desaparecidos_count": [int(d_count_map.get(dt, 0)) if dt in d_count_map else None for dt in all_dates],
        }

    # Build analysis
    analyses = build_analyses(df_h, df_d, names)
    return {"states": states, "series": data, "analyses": analyses}


# ---------------------------------------------------------------------------
# Analysis generation
# ---------------------------------------------------------------------------

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _fmt_date(fecha):
    """Format a pandas Timestamp into 'mes de YYYY'."""
    return f"{MESES[fecha.month - 1]} de {fecha.year}"


def _pct(old, new):
    if old == 0:
        return None
    return ((new - old) / old) * 100


def _trend_word(pct):
    if pct is None:
        return None
    if pct <= -15:
        return "una caida significativa"
    if pct <= -5:
        return "una disminucion"
    if pct < 5:
        return "relativa estabilidad"
    if pct < 15:
        return "un incremento"
    return "un incremento significativo"


def _state_stats(df, clave):
    """Compute summary stats for one series of one state."""
    s = df[df["clave_ent"] == clave].sort_values("fecha").copy()
    if s.empty:
        return None

    s = s[s["tasa_100k"].notna()]
    if len(s) < 13:
        return None

    last = s.iloc[-1]
    prev_year = s[s["fecha"] == last["fecha"] - pd.DateOffset(months=12)]
    yoy = None
    if not prev_year.empty:
        yoy = _pct(prev_year.iloc[0]["tasa_100k"], last["tasa_100k"])

    peak = s.loc[s["tasa_100k"].idxmax()]

    # Trend: avg of last 12 months vs prior 12 months
    last12 = s.tail(12)["tasa_100k"].mean()
    prior12 = s.iloc[-(min(24, len(s))):-12]["tasa_100k"].mean() if len(s) >= 24 else None
    trend_pct = _pct(prior12, last12) if prior12 else None

    # Cumulative last 12 months count
    total_12m = int(s.tail(12)["count"].sum())

    return {
        "last_rate": round(last["tasa_100k"], 2),
        "last_count": int(last["count"]),
        "last_date": _fmt_date(last["fecha"]),
        "yoy": round(yoy, 1) if yoy is not None else None,
        "peak_rate": round(peak["tasa_100k"], 2),
        "peak_date": _fmt_date(peak["fecha"]),
        "trend_pct": round(trend_pct, 1) if trend_pct is not None else None,
        "avg_12m": round(last12, 2),
        "total_12m": total_12m,
    }


def build_analyses(df_h, df_d, names):
    """Generate per-state analytical text."""
    # Nacional stats for comparisons
    nac_h = _state_stats(df_h, 0)
    nac_d = _state_stats(df_d, 0)

    # Rank states by latest rate (excluding Nacional)
    state_claves = [c for c in names if c != 0]
    h_ranks = {}
    d_ranks = {}
    for c in state_claves:
        sh = _state_stats(df_h, c)
        sd = _state_stats(df_d, c)
        if sh:
            h_ranks[c] = sh["last_rate"]
        if sd:
            d_ranks[c] = sd["last_rate"]
    h_sorted = sorted(h_ranks, key=h_ranks.get, reverse=True)
    d_sorted = sorted(d_ranks, key=d_ranks.get, reverse=True)
    h_rank_map = {c: i + 1 for i, c in enumerate(h_sorted)}
    d_rank_map = {c: i + 1 for i, c in enumerate(d_sorted)}

    analyses = {}
    for clave, name in names.items():
        sh = _state_stats(df_h, clave)
        sd = _state_stats(df_d, clave)
        if not sh or not sd:
            analyses[name] = []
            continue

        bullets = []
        is_nac = clave == 0

        # --- Bullet 1: Homicidios current level + trend ---
        b1 = f"En {sh['last_date']}, "
        if is_nac:
            b1 += f"la tasa nacional de homicidios dolosos fue de <strong>{sh['last_rate']}</strong> por cada 100,000 habitantes"
        else:
            b1 += f"la tasa de homicidios dolosos en {name} fue de <strong>{sh['last_rate']}</strong> por cada 100,000 habitantes"
        if sh["yoy"] is not None:
            direction = "menor" if sh["yoy"] < 0 else "mayor"
            b1 += f", <strong>{abs(sh['yoy']):.1f}%</strong> {direction} que el mismo mes del ano anterior"
        b1 += "."
        if sh["trend_pct"] is not None:
            tw = _trend_word(sh["trend_pct"])
            if tw:
                b1 += f" El promedio de los ultimos 12 meses muestra {tw} ({sh['trend_pct']:+.1f}%) respecto al periodo previo."
        bullets.append(b1)

        # --- Bullet 2: Desaparecidos current level + trend ---
        b2 = f"La tasa de personas desaparecidas y no localizadas "
        if is_nac:
            b2 += f"a nivel nacional se ubico en <strong>{sd['last_rate']}</strong> por cada 100,000 habitantes en {sd['last_date']}"
        else:
            b2 += f"en {name} se ubico en <strong>{sd['last_rate']}</strong> por cada 100,000 habitantes en {sd['last_date']}"
        if sd["yoy"] is not None:
            direction = "por debajo" if sd["yoy"] < 0 else "por encima"
            b2 += f", <strong>{abs(sd['yoy']):.1f}%</strong> {direction} del nivel registrado un ano antes"
        b2 += "."
        if sd["trend_pct"] is not None:
            tw = _trend_word(sd["trend_pct"])
            if tw:
                b2 += f" La tendencia reciente refleja {tw} ({sd['trend_pct']:+.1f}%)."
        bullets.append(b2)

        # --- Bullet 3: Peak comparison ---
        from_peak_h = _pct(sh["peak_rate"], sh["last_rate"])
        from_peak_d = _pct(sd["peak_rate"], sd["last_rate"])
        b3 = ""
        if from_peak_h is not None and from_peak_h < -10:
            b3 += f"El punto mas critico en homicidios se alcanzo en {sh['peak_date']} ({sh['peak_rate']} por 100k); el nivel actual es <strong>{abs(from_peak_h):.0f}%</strong> inferior."
        elif from_peak_h is not None and abs(from_peak_h) <= 10:
            b3 += f"La tasa de homicidios se encuentra cerca de su maximo historico, registrado en {sh['peak_date']} ({sh['peak_rate']} por 100k)."
        if from_peak_d is not None and from_peak_d < -10:
            if b3:
                b3 += " "
            b3 += f"En desapariciones, el pico fue en {sd['peak_date']} ({sd['peak_rate']} por 100k), <strong>{abs(from_peak_d):.0f}%</strong> por encima del dato mas reciente."
        if b3:
            bullets.append(b3)

        # --- Bullet 4: State vs. national comparison (skip for Nacional) ---
        if not is_nac and nac_h and nac_d:
            ratio_h = sh["last_rate"] / nac_h["last_rate"] if nac_h["last_rate"] > 0 else 0
            ratio_d = sd["last_rate"] / nac_d["last_rate"] if nac_d["last_rate"] > 0 else 0
            rank_h = h_rank_map.get(clave)
            rank_d = d_rank_map.get(clave)
            n_states = len(state_claves)

            parts = []
            if ratio_h > 1.3:
                parts.append(f"homicidios <strong>{ratio_h:.1f}x</strong> la media nacional")
            elif ratio_h < 0.7:
                parts.append(f"homicidios <strong>{(1 - ratio_h) * 100:.0f}%</strong> por debajo de la media nacional")
            if ratio_d > 1.3:
                parts.append(f"desapariciones <strong>{ratio_d:.1f}x</strong> la media nacional")
            elif ratio_d < 0.7:
                parts.append(f"desapariciones <strong>{(1 - ratio_d) * 100:.0f}%</strong> por debajo de la media nacional")

            b4 = f"{name} ocupa el lugar <strong>{rank_h}</strong> de {n_states} entidades en homicidios"
            b4 += f" y el <strong>{rank_d}</strong> en desapariciones."
            if parts:
                b4 += " Presenta " + " y ".join(parts) + "."
            bullets.append(b4)

        # --- Bullet 5: Absolute numbers in last 12 months ---
        b5 = f"En los ultimos 12 meses se registraron <strong>{sh['total_12m']:,}</strong> homicidios dolosos"
        b5 += f" y <strong>{sd['total_12m']:,}</strong> personas desaparecidas y no localizadas"
        if is_nac:
            b5 += " en todo el pais."
        else:
            b5 += f" en {name}."
        bullets.append(b5)

        analyses[name] = bullets

    return analyses


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seguridad en Mexico</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root {
  --red: #dc2626;
  --red-light: rgba(220,38,38,0.08);
  --blue: #2563eb;
  --blue-light: rgba(37,99,235,0.08);
  --bg: #f8fafc;
  --surface: #ffffff;
  --border: #e2e8f0;
  --text: #0f172a;
  --text-secondary: #64748b;
  --text-muted: #94a3b8;
  --radius: 12px;
  --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}

/* Header */
.header {
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  color: white;
  padding: 2.5rem 1.5rem 2rem;
}
.header-inner {
  max-width: 1040px;
  margin: 0 auto;
}
.header h1 {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin-bottom: 0.35rem;
}
.header p {
  color: #94a3b8;
  font-size: 0.875rem;
  font-weight: 400;
}

/* Main container */
.container {
  max-width: 1040px;
  margin: 0 auto;
  padding: 0 1.5rem 3rem;
}

/* Controls bar */
.controls {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin: -1.25rem 0 1.5rem;
  position: relative;
  z-index: 10;
}
.select-wrapper {
  position: relative;
}
.select-wrapper select {
  appearance: none;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.625rem 2.5rem 0.625rem 1rem;
  font-family: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--text);
  cursor: pointer;
  box-shadow: var(--shadow-md);
  transition: border-color 0.15s, box-shadow 0.15s;
  min-width: 220px;
}
.select-wrapper select:hover { border-color: #cbd5e1; }
.select-wrapper select:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,0.12); }
.select-wrapper::after {
  content: '';
  position: absolute;
  right: 14px;
  top: 50%;
  transform: translateY(-50%);
  width: 0; height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 5px solid var(--text-secondary);
  pointer-events: none;
}

/* KPI Cards */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem 1.25rem 1rem;
  box-shadow: var(--shadow);
  transition: box-shadow 0.15s;
}
.kpi-card:hover { box-shadow: var(--shadow-md); }
.kpi-card.red { border-top: 3px solid var(--red); }
.kpi-card.blue { border-top: 3px solid var(--blue); }
.kpi-card.neutral { border-top: 3px solid var(--text-muted); }
.kpi-label {
  font-size: 0.75rem;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
  margin-bottom: 0.5rem;
}
.kpi-value {
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.1;
}
.kpi-value.red { color: var(--red); }
.kpi-value.blue { color: var(--blue); }
.kpi-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.35rem;
}
.kpi-change {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
  margin-top: 0.4rem;
}
.kpi-change.up { color: #dc2626; background: rgba(220,38,38,0.08); }
.kpi-change.down { color: #16a34a; background: rgba(22,163,74,0.08); }

/* Chart panel */
.chart-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem;
  box-shadow: var(--shadow);
  margin-bottom: 1.5rem;
}
.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 1rem;
}
.chart-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text);
}
.chart-subtitle {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 0.15rem;
}
.chart-legend {
  display: flex;
  gap: 1.25rem;
  font-size: 0.8rem;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 0.35rem;
  vertical-align: middle;
}
.legend-dot.red { background: var(--red); }
.legend-dot.blue { background: var(--blue); }
.chart-container {
  position: relative;
  width: 100%;
  height: 420px;
}

/* Analysis panel */
.analysis-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem;
  box-shadow: var(--shadow);
  margin-bottom: 1.5rem;
}
.analysis-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.analysis-icon {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}
.analysis-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text);
}
.analysis-label {
  font-size: 0.65rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.15rem 0.45rem;
  margin-left: auto;
}
.analysis-bullets {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.analysis-bullets li {
  font-size: 0.85rem;
  line-height: 1.65;
  color: var(--text-secondary);
  padding-left: 1.1rem;
  position: relative;
}
.analysis-bullets li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0.55rem;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--border);
}
.analysis-bullets li:first-child::before { background: var(--red); }
.analysis-bullets li:nth-child(2)::before { background: var(--blue); }
.analysis-bullets li strong {
  color: var(--text);
  font-weight: 600;
}

/* Footer */
.footer {
  font-size: 0.78rem;
  color: var(--text-muted);
  border-top: 1px solid var(--border);
  padding-top: 1rem;
  line-height: 1.7;
}
.footer a { color: var(--text-secondary); text-decoration: none; }
.footer a:hover { text-decoration: underline; }

/* Responsive */
@media (max-width: 640px) {
  .header { padding: 2rem 1rem 1.5rem; }
  .header h1 { font-size: 1.25rem; }
  .container { padding: 0 1rem 2rem; }
  .kpi-grid { grid-template-columns: 1fr 1fr; }
  .chart-container { height: 300px; }
  .chart-header { flex-direction: column; gap: 0.5rem; }
  .controls { margin-top: -1rem; }
}
</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <h1>Seguridad en Mexico</h1>
    <p>Homicidios dolosos y personas desaparecidas &middot; Tasa mensual por 100,000 habitantes</p>
  </div>
</div>

<div class="container">
  <div class="controls">
    <div class="select-wrapper">
      <select id="state-select"></select>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi-card red">
      <div class="kpi-label">Homicidios dolosos</div>
      <div class="kpi-value red" id="kpi-hom-rate">--</div>
      <div class="kpi-sub" id="kpi-hom-sub"></div>
      <div class="kpi-change" id="kpi-hom-change"></div>
    </div>
    <div class="kpi-card blue">
      <div class="kpi-label">Personas desaparecidas</div>
      <div class="kpi-value blue" id="kpi-des-rate">--</div>
      <div class="kpi-sub" id="kpi-des-sub"></div>
      <div class="kpi-change" id="kpi-des-change"></div>
    </div>
    <div class="kpi-card neutral">
      <div class="kpi-label">Periodo de datos</div>
      <div class="kpi-value" id="kpi-period" style="font-size:1.1rem;color:var(--text);">--</div>
      <div class="kpi-sub" id="kpi-period-sub"></div>
    </div>
  </div>

  <div class="chart-panel">
    <div class="chart-header">
      <div>
        <div class="chart-title" id="chart-title">Nacional</div>
        <div class="chart-subtitle">Tasa mensual por 100,000 habitantes</div>
      </div>
      <div class="chart-legend">
        <span><span class="legend-dot red"></span>Homicidios dolosos</span>
        <span><span class="legend-dot blue"></span>Desaparecidos</span>
      </div>
    </div>
    <div class="chart-container">
      <canvas id="main-chart"></canvas>
    </div>
  </div>

  <div class="analysis-panel">
    <div class="analysis-header">
      <svg class="analysis-icon" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M9 2a1 1 0 0 1 2 0v1a1 1 0 0 1-2 0V2Zm-5.657 2.05a1 1 0 0 1 1.414 0l.707.707A1 1 0 0 1 4.05 6.172l-.707-.707a1 1 0 0 1 0-1.414ZM14 5.464a1 1 0 0 1 0-1.414l.707-.707a1 1 0 1 1 1.414 1.414l-.707.707A1 1 0 0 1 14 5.464ZM2 10a1 1 0 0 1 1-1h1a1 1 0 1 1 0 2H3a1 1 0 0 1-1-1Zm14 0a1 1 0 0 1 1-1h1a1 1 0 1 1 0 2h-1a1 1 0 0 1-1-1Zm-4 0a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm-2 4a4 4 0 0 0-3.465 2H6a1 1 0 1 0 0 2h8a1 1 0 1 0 0-2h-.535A4 4 0 0 0 10 14Z" fill="#94a3b8"/>
      </svg>
      <span class="analysis-title">Hallazgos clave</span>
      <span class="analysis-label">Auto-generado</span>
    </div>
    <ul class="analysis-bullets" id="analysis-list"></ul>
  </div>

  <div class="footer">
    Fuentes:
    <a href="https://www.gob.mx/sesnsp/acciones-y-programas/incidencia-delictiva-del-fuero-comun-nueva-metodologia" target="_blank">SESNSP &mdash; Incidencia delictiva</a> &middot;
    <a href="https://versionpublicarnpdno.segob.gob.mx/" target="_blank">SEGOB &mdash; RNPDNO</a> &middot;
    Poblacion: <a href="https://www.gob.mx/conapo" target="_blank">CONAPO</a>
  </div>
</div>

<script>
const DATA = __DATA_JSON__;

const MONTHS_ES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];

function formatDate(d) {
  const dt = new Date(d);
  return MONTHS_ES[dt.getMonth()] + ' ' + dt.getFullYear();
}

function formatRate(v) {
  if (v == null) return '--';
  return v.toFixed(2);
}

function formatCount(v) {
  if (v == null) return '--';
  return v.toLocaleString('es-MX');
}

// Find last valid index for a series
function lastValid(arr) {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] != null) return i;
  }
  return -1;
}

// Compute YoY change
function yoyChange(arr, idx) {
  if (idx < 12 || arr[idx] == null || arr[idx - 12] == null) return null;
  if (arr[idx - 12] === 0) return null;
  return ((arr[idx] - arr[idx - 12]) / arr[idx - 12]) * 100;
}

// Populate dropdown
const select = document.getElementById('state-select');
DATA.states.forEach(s => {
  const opt = document.createElement('option');
  opt.value = s;
  opt.textContent = s;
  select.appendChild(opt);
});

// Chart setup
const ctx = document.getElementById('main-chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      {
        label: 'Homicidios dolosos',
        data: [],
        borderColor: '#dc2626',
        backgroundColor: 'rgba(220,38,38,0.06)',
        fill: true,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#dc2626',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
        tension: 0.3,
        yAxisID: 'y',
        spanGaps: true,
      },
      {
        label: 'Personas desaparecidas',
        data: [],
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37,99,235,0.06)',
        fill: true,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: '#2563eb',
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
        tension: 0.3,
        yAxisID: 'y1',
        spanGaps: true,
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    interaction: {
      mode: 'index',
      intersect: false,
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(15,23,42,0.92)',
        titleFont: { family: 'Inter', size: 13, weight: '600' },
        bodyFont: { family: 'Inter', size: 12 },
        padding: { top: 10, bottom: 10, left: 14, right: 14 },
        cornerRadius: 8,
        displayColors: true,
        boxWidth: 10,
        boxHeight: 10,
        boxPadding: 4,
        callbacks: {
          title: function(items) {
            if (!items.length) return '';
            return formatDate(items[0].label);
          },
          label: function(item) {
            const state = select.value;
            const d = DATA.series[state];
            const idx = item.dataIndex;
            const rate = item.parsed.y;
            if (item.datasetIndex === 0) {
              const count = d.homicidios_count[idx];
              return ' Homicidios: ' + formatRate(rate) + ' (' + formatCount(count) + ' casos)';
            } else {
              const count = d.desaparecidos_count[idx];
              return ' Desaparecidos: ' + formatRate(rate) + ' (' + formatCount(count) + ' casos)';
            }
          }
        }
      }
    },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'year', tooltipFormat: 'MMM yyyy' },
        grid: { display: false },
        ticks: {
          font: { family: 'Inter', size: 11 },
          color: '#94a3b8',
        },
        border: { display: false },
      },
      y: {
        position: 'left',
        title: {
          display: true,
          text: 'Homicidios dolosos',
          font: { family: 'Inter', size: 11, weight: '500' },
          color: '#dc2626',
        },
        grid: { color: 'rgba(0,0,0,0.04)', drawBorder: false },
        ticks: {
          font: { family: 'Inter', size: 11 },
          color: '#94a3b8',
        },
        border: { display: false },
        beginAtZero: true,
      },
      y1: {
        position: 'right',
        title: {
          display: true,
          text: 'Desaparecidos',
          font: { family: 'Inter', size: 11, weight: '500' },
          color: '#2563eb',
        },
        grid: { drawOnChartArea: false },
        ticks: {
          font: { family: 'Inter', size: 11 },
          color: '#94a3b8',
        },
        border: { display: false },
        beginAtZero: true,
      }
    }
  }
});

function updateDashboard(state) {
  const d = DATA.series[state];
  if (!d) return;

  // Update chart
  chart.data.labels = d.dates;
  chart.data.datasets[0].data = d.homicidios;
  chart.data.datasets[1].data = d.desaparecidos;
  chart.update();

  // Update title
  document.getElementById('chart-title').textContent = state;

  // KPIs — use last valid data point
  const hIdx = lastValid(d.homicidios);
  const dIdx = lastValid(d.desaparecidos);

  if (hIdx >= 0) {
    document.getElementById('kpi-hom-rate').textContent = formatRate(d.homicidios[hIdx]);
    document.getElementById('kpi-hom-sub').textContent =
      formatDate(d.dates[hIdx]) + ' · ' + formatCount(d.homicidios_count[hIdx]) + ' casos';
    const hChange = yoyChange(d.homicidios, hIdx);
    const hEl = document.getElementById('kpi-hom-change');
    if (hChange != null) {
      const arrow = hChange > 0 ? '\\u2191' : '\\u2193';
      hEl.textContent = arrow + ' ' + Math.abs(hChange).toFixed(1) + '% vs. mismo mes a\\u00f1o anterior';
      hEl.className = 'kpi-change ' + (hChange > 0 ? 'up' : 'down');
    } else {
      hEl.textContent = '';
      hEl.className = 'kpi-change';
    }
  }

  if (dIdx >= 0) {
    document.getElementById('kpi-des-rate').textContent = formatRate(d.desaparecidos[dIdx]);
    document.getElementById('kpi-des-sub').textContent =
      formatDate(d.dates[dIdx]) + ' · ' + formatCount(d.desaparecidos_count[dIdx]) + ' casos';
    const dChange = yoyChange(d.desaparecidos, dIdx);
    const dEl = document.getElementById('kpi-des-change');
    if (dChange != null) {
      const arrow = dChange > 0 ? '\\u2191' : '\\u2193';
      dEl.textContent = arrow + ' ' + Math.abs(dChange).toFixed(1) + '% vs. mismo mes a\\u00f1o anterior';
      dEl.className = 'kpi-change ' + (dChange > 0 ? 'up' : 'down');
    } else {
      dEl.textContent = '';
      dEl.className = 'kpi-change';
    }
  }

  // Period
  const firstDate = d.dates[0];
  const lastDate = d.dates[d.dates.length - 1];
  document.getElementById('kpi-period').textContent = formatDate(firstDate) + ' \\u2014 ' + formatDate(lastDate);
  document.getElementById('kpi-period-sub').textContent = d.dates.length + ' meses de datos';

  // Analysis
  const bullets = DATA.analyses[state] || [];
  const list = document.getElementById('analysis-list');
  list.innerHTML = '';
  bullets.forEach(b => {
    const li = document.createElement('li');
    li.innerHTML = b;
    list.appendChild(li);
  });
}

select.addEventListener('change', () => updateDashboard(select.value));
updateDashboard('Nacional');
</script>
</body>
</html>
"""


def write_html(payload):
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_HTML}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading incidencia delictiva...")
    df_h = load_incidencia()
    print(f"  Homicidios dolosos: {len(df_h)} rows")

    print("Loading RNPDNO desaparecidos...")
    df_d = load_rnpdno()
    print(f"  Desaparecidos: {len(df_d)} rows")

    pob = download_conapo()
    print(f"  Population records: {len(pob)}")

    print("Processing series...")
    df_h = process_series(df_h, pob)
    df_d = process_series(df_d, pob)

    print("Building JSON payload...")
    payload = build_json_payload(df_h, df_d)
    print(f"  States: {len(payload['states'])}")

    write_html(payload)
    print("Done!")


if __name__ == "__main__":
    main()
