# nic-ar-rdlist

Dataset histórico normalizado de la **RDList de NIC Argentina**, almacenado en
una base de datos [DuckDB](https://duckdb.org/).

El proyecto descubre los listados publicados en
[rdlist.nic.ar](https://rdlist.nic.ar/rd_list/), descarga los CSV, verifica cada
uno contra el hash SHA-256 publicado por NIC y los importa de forma transaccional.
Sobre esos registros se pueden escanear los dominios para capturar metadatos de
sus páginas y clasificarlos con un modelo de decisiones.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (gestiona el intérprete e instala las dependencias).
- Python 3.12 (lo instala uv a partir de `.python-version`).

## Instalación

```bash
uv sync
```

Esto crea `.venv` e instala las dependencias de ejecución y `dev` según `uv.lock`.

## Uso

Todos los comandos se ejecutan con `uv run`, que vuelve a sincronizar el entorno
automáticamente.

### Actualizar la base de datos

```bash
# Descarga e importa el historial publicado completo.
uv run python scripts/update_db.py bootstrap

# Actualiza las tres páginas de listado más recientes.
uv run python scripts/update_db.py latest

# Solo migración de esquema, sin acceso a la red.
uv run python scripts/update_db.py migrate
```

Opciones útiles: `--database PATH` para operar sobre otra base de datos,
`--recent-pages N` para ampliar la ventana de `latest` y `--log-level DEBUG`
para diagnóstico. La verificación TLS está habilitada por defecto; `--insecure`
es solo para diagnóstico local y `--ca-bundle` permite confiar en una CA
personalizada.

El comando imprime `changed=true` o `changed=false` al finalizar.

### Escanear dominios

Sondea cada dominio una sola vez (HTTPS primero, HTTP como respaldo) y captura
disponibilidad, esquema, estado, URL final y metadatos de la página.

```bash
uv run python scripts/scan_domains.py 2026-09
```

Solo se requiere el período (`YYYY`, `YYYY-MM` o `YYYY-MM-DD`). Los dominios se
leen de `domain_records`, el resultado se escribe en `data/meta-<period>.csv` y
luego se carga en la tabla `domain_pages`. Use `--input` para una lista o CSV
propios, `--limit N` para acotar los dominios pendientes y `--workers`/`--timeout`
para ajustar la concurrencia.

### Clasificar dominios

Clasifica cada dominio en un tipo de sitio y un sector usando TypeSafe Jev a
través de la API Decisions de OpenRouter.

```bash
uv run python scripts/classify_domains.py 2026-09
```

Lee los candidatos escaneados de `domain_pages`, escribe
`data/categories-<period>.csv` y los carga en la tabla `domain_categories`. La
clave de API se resuelve, en orden, desde `--api-key`, la variable de entorno
`OPENROUTER_API_KEY` o la credencial `openrouter` almacenada por OpenCode.

### Exportar a Parquet

```bash
uv run python scripts/export_parquet.py --database data/rdlist.duckdb --output data/rdlist.parquet
```

## Modelo de datos

| Objeto | Descripción |
| --- | --- |
| `sources` | Un CSV de NIC validado por fecha de origen, con su hash publicado y descargado. |
| `domain_records` | Las filas normalizadas del registro de dominios. |
| `dataset_status` | Vista con el rango de fechas, la cantidad de archivos y de registros. |
| `domain_pages` | Metadatos de las páginas escaneadas (se crea con `scan_domains.py`). |
| `domain_categories` | Clasificación tipo de sitio y sector (se crea con `classify_domains.py`). |

## Pruebas

```bash
uv run pytest
```

Las pruebas están bajo `tests/` y no requieren acceso a la red.

## Estructura del proyecto

- `scripts/update_db.py`: punto de entrada de la ingesta.
- `scripts/scan_domains.py`: sondeo y captura de metadatos de dominios.
- `scripts/classify_domains.py`: clasificación con la API Decisions.
- `scripts/export_parquet.py`: exportación de `domain_records` a Parquet.
- `scripts/periods.py`: utilidades de períodos compartidas.
- `.github/workflows/update-db.yml`: actualización programada contra Cloudflare R2.
- `data/`: ubicación local de `rdlist.duckdb` (generada e ignorada por git).

## Seguridad

- La verificación TLS se mantiene habilitada por defecto.
- Cada CSV se valida contra el SHA-256 publicado por NIC antes de importarse.
- El reemplazo de fuentes es atómico y se conserva la consistencia entre
  `sources` y `domain_records`.
- No se versionan bases DuckDB generadas, credenciales, cachés ni configuración
  de R2.
