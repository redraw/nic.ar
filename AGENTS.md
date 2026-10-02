# Guías del repositorio

## Estructura del proyecto y organización de módulos

Este repositorio mantiene el dataset histórico RDList de NIC Argentina como una base de datos DuckDB normalizada.

- `scripts/update_db.py` es el punto de entrada de la aplicación. Descubre los listados de RDList, descarga los CSV, verifica sus hashes SHA-256 publicados y los importa de forma transaccional.
- `data/` es la ubicación de trabajo local para `rdlist.duckdb`. Los archivos de base de datos se generan y se ignoran; conserve solo `data/.gitkeep`.
- `.github/workflows/update-db.yml` ejecuta la actualización programada o manual. Restaura la base de datos canónica desde Cloudflare R2, la actualiza y la sube solo cuando el dataset cambió.
- `pyproject.toml` declara las dependencias de ejecución (DuckDB, Requests, tqdm) y el grupo de dependencias `dev` (`pytest`); `uv.lock` fija las versiones resueltas exactas y se versiona.
- `.python-version` fija el intérprete (`3.12`) que uv instala y usa.

La base de datos contiene `sources` (un CSV de NIC validado por fecha de origen), `domain_records` (las filas normalizadas) y la vista `dataset_status`. Los datos heredados de `records` se migran a `domain_records` y luego se eliminan.

## Comandos de compilación, prueba y desarrollo

Cree y complete el entorno gestionado con `uv sync`, que crea `.venv`, resuelve contra `uv.lock` e instala tanto las dependencias de ejecución como las `dev`. Para ejecutar comandos, antepóngalos con `uv run`, que vuelve a sincronizar el entorno automáticamente.

- `uv sync` instala las dependencias bloqueadas de ejecución y `dev` en `.venv` (agregue `--no-dev` para solo ejecución).
- `uv lock` vuelve a resolver dependencias y actualiza `uv.lock`; ejecútelo después de cambiar `pyproject.toml` y haga commit del resultado.
- `uv sync --frozen` instala exactamente lo que especifica `uv.lock` sin volver a resolver; CI usa esto mediante `uv run --frozen`.
- `uv run python scripts/update_db.py bootstrap` descarga e importa el historial publicado completo en `data/rdlist.duckdb`.
- `uv run python scripts/update_db.py latest` actualiza las tres páginas de listado más recientes; pase `--recent-pages N` para cambiar esa ventana.
- `uv run python scripts/update_db.py migrate` realiza solo la migración de esquema sin conexión.
- `uv run pytest` ejecuta las pruebas unitarias bajo `tests/`.
- Agregue `--database PATH` para operar sobre una base de datos temporal o alternativa, y `--log-level DEBUG` para registro de diagnóstico.

El actualizador termina imprimiendo `changed=true` o `changed=false`; el flujo de GitHub Actions depende de esa salida exacta. No hay aplicación JavaScript ni comando de compilación de frontend; `tests/` contiene la suite de pytest.

## Estilo de código y convenciones de nombres

Use código compatible con Python 3.12, indentación de cuatro espacios, anotaciones de tipo, nombres en `snake_case` y docstrings concisos. Siga el estilo compacto que ya se usa en `scripts/update_db.py`.

Preserve las garantías de ingesta: mantenga la verificación TLS habilitada por defecto, verifique cada CSV descargado contra el SHA-256 publicado por NIC antes de importarlo y mantenga atómico el reemplazo de fuentes. Trate `--insecure` solo como una solución temporal de diagnóstico local; use `--ca-bundle` para una CA personalizada de confianza.

Mantenga `sources` y `domain_records` consistentes para cada `source_date`. Los cambios de esquema pertenecen a `ensure_schema`/`_ensure_schema` y deben ser seguros para bases de datos existentes. Normalice los valores de dominio y zona de forma consistente (recortar, quitar puntos externos, minúsculas) y evite convertir valores faltantes en cadenas vacías engañosas.

## Guías de prueba

Para cambios en el análisis, la normalización, la migración o la lógica de importación, ejercite el modo más pequeño apropiado contra una base de datos temporal usando `--database`; no sobrescriba `data/rdlist.duckdb` solo para validar un cambio. Ejecute las pruebas unitarias con `uv run pytest`. Agregue cobertura de pytest enfocada bajo `tests/` al introducir o cambiar comportamiento, preferiblemente usando bases de datos `tmp_path` y respuestas HTTP simuladas.

## Guías de commits y pull requests

Use asuntos de commit imperativos y concisos. Mantenga los commits enfocados y describa en las pull requests el impacto en el modelo de datos, la ingesta o la automatización, incluidos los comandos ejecutados. No haga commit de archivos DuckDB generados, credenciales, cachés ni configuración de R2.

## Seguridad y configuración

El flujo programado se ejecuta diariamente a las 08:00 UTC y puede despacharse con `latest` o `bootstrap`. Requiere estos valores de configuración de GitHub para R2:

- Variables: `R2_BUCKET`, `R2_ACCOUNT_ID`
- Secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`

Mantenga intacto el flujo de restaurar-actualizar-subir para que las actualizaciones rutinarias sean incrementales y un objeto canónico ausente haga bootstrap correctamente.
