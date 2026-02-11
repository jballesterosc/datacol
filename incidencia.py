import pandas as pd
import altair as alt
import io
import os
import requests

url = "https://repodatos.atdt.gob.mx/api_update/sesnsp/incidencia_delictiva/INM_estatal_nov25.csv"
headers = {"User-Agent": "Mozilla/5.0"}
resp = requests.get(url, headers=headers)
resp.raise_for_status()

df = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8")

url_1 = "https://repodatos.atdt.gob.mx/CONAPO/proyecciones/00_Pob_Inicio_1950_2070.csv"
headers = {"User-Agent": "Mozilla/5.0"}
resp = requests.get(url_1, headers=headers)
resp.raise_for_status()

poblacion = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8")

# Sumar población por entidad y año (todas las edades y sexos)
poblacion_ag = (
    poblacion.groupby(["ANIO", "ENTIDAD"], as_index=False)["POBLACION"]
    .sum()
    .rename(columns={"ANIO": "anio", "ENTIDAD": "entidad"})
)
poblacion_ag.head()

# Agregar por subtipo_delito (eliminar modalidad) y agregar poblacion
df = (
    df.groupby(["anio", "entidad", "clave_ent", "entidad_federativa",
               "bien_juridico_afectado", "tipo_delito", "subtipo_delito",
               "mes", "fecha"], as_index=False)
    ["incidencia_delictiva"].sum()
)

# Merge con poblacion
df = df.merge(poblacion_ag, on=["anio", "entidad"], how="left")
df = df.rename(columns={"POBLACION": "poblacion"})

# Tasa por 100k habitantes
df["tasa_100k"] = ((df["incidencia_delictiva"] / df["poblacion"]) * 100000).round(2)

# Promedios móviles mensuales por entidad y subtipo de delito
df = df.sort_values(["entidad_federativa", "subtipo_delito", "fecha"])
grupo = df.groupby(["entidad_federativa", "subtipo_delito"])

for n in [3, 6, 12]:
    df[f"incidencia_ma{n}"] = grupo["incidencia_delictiva"].transform(lambda x: x.rolling(n, min_periods=1).mean()).round(2)
    df[f"tasa_100k_ma{n}"] = grupo["tasa_100k"].transform(lambda x: x.rolling(n, min_periods=1).mean()).round(2)


os.makedirs("_data", exist_ok=True)
df.to_csv("_data/incidencia_delictiva.csv", index=False)
print(f"Exportado: {len(df)} filas a _data/incidencia_delictiva.csv")