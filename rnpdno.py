"""
Scraper for RNPDNO (Registro Nacional de Personas Desaparecidas y No Localizadas).

Extracts monthly time-series data on missing persons in Mexico from the SEGOB
dashboard, disaggregated by state and category.

Strategy:
  The ContextoGeneral page's API endpoints ignore date filters. However, the
  Sociodemografico page's endpoints (BarChartSexoEstados) accept date and
  status filters via POST JSON params. We:
  1. Accept terms on the landing page
  2. Navigate to the Sociodemografico page to get a valid session
  3. For each month × each status category, call BarChartSexoEstados
  4. Sum Hombre + Mujer + Indeterminado per state to get totals
  5. Compute the combined "desaparecidas y no localizadas" category

Usage:
    pip install playwright pandas
    playwright install chromium
    python rnpdno.py
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import pandas as pd
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://versionpublicarnpdno.segob.gob.mx"
LANDING_URL = f"{BASE_URL}/"
SOCIO_URL = f"{BASE_URL}/Dashboard/Sociodemografico"
CHART_URL = "/SocioDemografico/BarChartSexoEstados"

# Status values -> category names (matching incidencia.py conventions)
STATUS_CATEGORIES = {
    "4": "Personas desaparecidas",
    "5": "Personas no localizadas",
    "2": "Personas localizadas con vida",
    "3": "Personas localizadas sin vida",
}
COMBINED_CATEGORY = "Personas desaparecidas y no localizadas"
COMBINED_STATUSES = ["4", "5"]

ENTIDADES = {
    1: "Aguascalientes",
    2: "Baja California",
    3: "Baja California Sur",
    4: "Campeche",
    5: "Coahuila de Zaragoza",
    6: "Colima",
    7: "Chiapas",
    8: "Chihuahua",
    9: "Ciudad de México",
    10: "Durango",
    11: "Guanajuato",
    12: "Guerrero",
    13: "Hidalgo",
    14: "Jalisco",
    15: "México",
    16: "Michoacán de Ocampo",
    17: "Morelos",
    18: "Nayarit",
    19: "Nuevo León",
    20: "Oaxaca",
    21: "Puebla",
    22: "Querétaro",
    23: "Quintana Roo",
    24: "San Luis Potosí",
    25: "Sinaloa",
    26: "Sonora",
    27: "Tabasco",
    28: "Tamaulipas",
    29: "Tlaxcala",
    30: "Veracruz de Ignacio de la Llave",
    31: "Yucatán",
    32: "Zacatecas",
}

# Dashboard state name variants -> INEGI code
ENTIDAD_NAME_TO_CODE = {}
for _code, _name in ENTIDADES.items():
    ENTIDAD_NAME_TO_CODE[_name.upper()] = _code
ENTIDAD_NAME_TO_CODE.update({
    "COAHUILA": 5,
    "CIUDAD DE MEXICO": 9,
    "ESTADO DE MEXICO": 15,
    "ESTADO DE MÉXICO": 15,
    "MEXICO": 15,
    "MÉXICO": 15,
    "MICHOACAN": 16,
    "MICHOACAN DE OCAMPO": 16,
    "NUEVO LEON": 19,
    "QUERETARO": 22,
    "SAN LUIS POTOSI": 24,
    "VERACRUZ": 30,
    "VERACRUZ DE IGNACIO DE LA LLAVE": 30,
    "YUCATAN": 31,
})

OUTPUT_PATH = "_data/rnpdno_desaparecidos.csv"
HEADLESS = True
REQUEST_DELAY_MS = 300
MAX_RETRIES = 3
START_YEAR = 2015


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_months(start_year, start_month=1):
    """Generate (year, month) tuples from start to current month."""
    now = datetime.now()
    year, month = start_year, start_month
    while (year, month) <= (now.year, now.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def last_day_of_month(year, month):
    """Return the last day of the given month."""
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

async def accept_terms(page):
    """Navigate to landing page and accept terms of service."""
    print("[1] Navigating to landing page...")
    await page.goto(LANDING_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    clicked = await page.evaluate("""() => {
        const els = [...document.querySelectorAll('a, button, input[type=submit]')];
        for (const el of els) {
            const text = (el.textContent || el.value || '').trim();
            if (text === 'Aceptar' || text === 'ACEPTAR') {
                el.click();
                return true;
            }
        }
        return false;
    }""")

    if clicked:
        print("  Clicked 'Aceptar', waiting for navigation...")
        await page.wait_for_load_state("networkidle", timeout=60000)
    else:
        print("  Accept button not found, navigating directly...")

    await page.wait_for_timeout(3000)


async def load_sociodemografico(page):
    """Load the Sociodemografico page to establish session context."""
    print("[2] Loading Sociodemografico page...")
    await page.goto(SOCIO_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(5000)

    # Dismiss any modals
    await page.evaluate("""() => {
        document.querySelectorAll('.modal-backdrop, .swal2-container').forEach(o => o.remove());
        document.body.classList.remove('modal-open');
        for (const btn of document.querySelectorAll('button')) {
            if (['Entendido', 'Cerrar'].includes(btn.textContent.trim())) btn.click();
        }
    }""")
    await page.wait_for_timeout(2000)

    # Verify jQuery is available
    has_jquery = await page.evaluate("() => !!(window.$ && window.$.ajax)")
    print(f"  jQuery available: {has_jquery}")
    if not has_jquery:
        raise RuntimeError("jQuery not available on Sociodemografico page")


async def call_bar_chart(page, status_id, fecha_inicio, fecha_fin):
    """
    Call /SocioDemografico/BarChartSexoEstados with given status and date range.
    Returns dict with state names as keys and total counts as values.
    """
    result = await page.evaluate("""([url, statusId, fechaIni, fechaFin]) => {
        return new Promise((resolve, reject) => {
            var obj = {
                titulo: '',
                idEstatusVictima: statusId,
                fechaInicio: fechaIni,
                fechaFin: fechaFin,
                idEstado: '0',
                idMunicipio: '0',
                mostrarFechaNula: '0',
                idColonia: '0',
                idNacionalidad: '',
                edadInicio: '',
                edadFin: '',
                mostrarEdadNula: '0',
                idHipotesis: '',
                idMedioConocimiento: '',
                idCircunstancia: '',
                tieneDiscapacidad: '',
                idTipoDiscapacidad: '',
                idEtnia: '',
                idLengua: '',
                idReligion: '',
                esMigrante: '',
                idEstatusMigratorio: '',
                esLgbttti: '',
                esServidorPublico: '',
                esDefensorDH: '',
                esPeriodista: '',
                esSindicalista: '',
                esONG: '',
                idHipotesisNoLocalizacion: '',
                idDelito: ''
            };
            $.ajax({
                type: "POST",
                url: url,
                data: JSON.stringify(obj),
                contentType: "application/json; charset=utf-8",
                dataType: "json",
                async: true
            }).done(function(data) {
                resolve(typeof data === 'string' ? data : JSON.stringify(data));
            }).fail(function(xhr, status, err) {
                reject(status + ':' + err + ':' + xhr.status);
            });
        });
    }""", [CHART_URL, status_id, fecha_inicio, fecha_fin])

    data = json.loads(result)
    categories = data.get("XAxisCategories", [])
    series_list = data.get("Series", [])

    state_totals = {}
    for i, state_name in enumerate(categories):
        total = 0
        for series in series_list:
            pts = series.get("data", [])
            if i < len(pts):
                pt = pts[i]
                if isinstance(pt, dict):
                    total += pt.get("value", 0) or pt.get("y", 0) or 0
                elif isinstance(pt, (int, float)):
                    total += pt
        state_totals[state_name.upper().strip()] = int(total)

    return state_totals


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

# Shared state for partial saves
_partial_records = []


async def extract_monthly_data(page):
    """Extract monthly data for all states and categories."""
    global _partial_records
    months = list(generate_months(START_YEAR))
    total_months = len(months)
    statuses = list(STATUS_CATEGORIES.keys())
    total_calls = total_months * len(statuses)

    print(f"\n[3] Extracting {total_months} months × {len(statuses)} statuses = {total_calls} API calls...")

    all_records = []
    _partial_records = all_records
    call_count = 0

    for idx, (year, month) in enumerate(months):
        last_day = last_day_of_month(year, month)
        fecha_ini = f"{1:02d}/{month:02d}/{year}"
        fecha_fin = f"{last_day:02d}/{month:02d}/{year}"
        fecha_csv = f"{year}-{month:02d}-01"

        month_data = {}  # {status_id: {state_upper: count}}

        for status_id in statuses:
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    state_totals = await call_bar_chart(page, status_id, fecha_ini, fecha_fin)
                    month_data[status_id] = state_totals
                    call_count += 1
                    break
                except Exception as e:
                    retries += 1
                    if retries >= MAX_RETRIES:
                        print(f"  FAILED {year}-{month:02d} status={status_id}: {e}")
                        month_data[status_id] = {}
                    else:
                        print(f"  Retry {retries} for {year}-{month:02d} status={status_id}: {e}")
                        await page.wait_for_timeout(3000 * retries)
                        try:
                            await accept_terms(page)
                            await load_sociodemografico(page)
                        except Exception as re_err:
                            print(f"  Re-auth failed: {re_err}, waiting 10s...")
                            await page.wait_for_timeout(10000)

            await page.wait_for_timeout(REQUEST_DELAY_MS)

        # Build records for this month
        month_records = 0
        all_state_names = set()
        for state_totals in month_data.values():
            all_state_names.update(state_totals.keys())

        for state_upper in all_state_names:
            clave_ent = ENTIDAD_NAME_TO_CODE.get(state_upper)
            if clave_ent is None:
                continue

            # Add each category
            for status_id, categoria in STATUS_CATEGORIES.items():
                val = month_data.get(status_id, {}).get(state_upper, 0)
                all_records.append({
                    "fecha": fecha_csv,
                    "anio": year,
                    "mes": month,
                    "clave_ent": clave_ent,
                    "entidad": ENTIDADES[clave_ent],
                    "categoria": categoria,
                    "valor": val,
                })
                month_records += 1

            # Compute combined category
            combined = sum(
                month_data.get(s, {}).get(state_upper, 0) for s in COMBINED_STATUSES
            )
            all_records.append({
                "fecha": fecha_csv,
                "anio": year,
                "mes": month,
                "clave_ent": clave_ent,
                "entidad": ENTIDADES[clave_ent],
                "categoria": COMBINED_CATEGORY,
                "valor": combined,
            })
            month_records += 1

        # Progress logging
        if (idx + 1) % 12 == 0 or idx == 0 or idx == total_months - 1:
            month_total = sum(
                month_data.get(s, {}).get(st, 0)
                for s in statuses
                for st in all_state_names
                if ENTIDAD_NAME_TO_CODE.get(st) is not None
            )
            print(f"  [{idx+1}/{total_months}] {year}-{month:02d}: "
                  f"{month_records} records, total={month_total}")

    print(f"\n  Total API calls: {call_count}")
    return all_records


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def save_to_csv(records):
    """Save records to CSV."""
    if not records:
        print("\nNo records to save!")
        return None

    df = pd.DataFrame(records)
    df = df.sort_values(["entidad", "categoria", "fecha"])
    df = df[["fecha", "anio", "mes", "clave_ent", "entidad", "categoria", "valor"]]

    os.makedirs("_data", exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"\nExportado: {len(df)} filas a {OUTPUT_PATH}")

    print(f"\nResumen:")
    print(f"  Entidades: {df['entidad'].nunique()}")
    print(f"  Categorías: {df['categoria'].nunique()}")
    for cat in sorted(df["categoria"].unique()):
        cat_sum = df.loc[df["categoria"] == cat, "valor"].sum()
        print(f"    - {cat}: {len(df[df['categoria'] == cat])} filas, total={cat_sum:,}")
    print(f"  Periodo: {df['anio'].min()}-{df['anio'].max()}")
    print(f"  Meses: {df[['anio','mes']].drop_duplicates().shape[0]}")
    print(f"  Total filas: {len(df)}")

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("=" * 60)
    print("RNPDNO Scraper - Personas Desaparecidas y No Localizadas")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        await accept_terms(page)
        await load_sociodemografico(page)

        # Verify endpoints work with a quick test
        print("\n[Check] Testing API with Jan 2024...")
        test = await call_bar_chart(page, "4", "01/01/2024", "31/01/2024")
        test_sum = sum(v for k, v in test.items() if ENTIDAD_NAME_TO_CODE.get(k))
        print(f"  Desaparecidos Jan 2024: {test_sum} across {len(test)} states")
        if test_sum == 0:
            print("  WARNING: API returned 0. Check session/connectivity.")

        try:
            records = await extract_monthly_data(page)
        except Exception as e:
            print(f"\nERROR during extraction: {e}")
            print("Saving partial results...")
            records = _partial_records
        finally:
            await browser.close()

    save_to_csv(records)


if __name__ == "__main__":
    asyncio.run(main())
