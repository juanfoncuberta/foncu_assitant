"""
Registro del consumo de IA (tokens y coste) para poder auditarlo.

No sabe de ningun proveedor: recibe la respuesta tal cual, se la pasa al
UsageProvider que corresponde (provider_factory.get_usage_provider) y guarda el
registro normalizado. Anadir un proveedor nuevo no toca este modulo.

Cada fila dice:
- provider: quien cobra ('anthropic', ...). Es lo que suma el desglose por proveedor.
- client:   por que via se llamo y con que parser se leyo ('anthropic_api',
            'claude_code'...): el nombre registrado en provider_factory.
- project:  a que proyecto se imputa el gasto ('foncu_assitant', 'linkedin'...).
            SIN_PROYECTO si no se sabe.
- step:     para que ('chat', 'summary', 'plan', 'exec', 'review').
- *_price_per_mtok: el precio por millon de tokens que se aplico en esa llamada.

Registrar nunca lanza excepcion: un fallo apuntando el gasto no puede tumbar la
respuesta al usuario ni una tarea de desarrollo. Se loguea y se sigue.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
import db_schema
import model_prices
import provider_factory

logger = logging.getLogger(__name__)

#: Etiqueta para el gasto que no se puede atribuir a ningun proyecto.
SIN_PROYECTO = "sin_proyecto"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path())
    # WAL: escriben a la vez el hilo del bot, los workers de asyncio.to_thread y el
    # executor de tareas de dev.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    db_schema.ensure_table(
        conn,
        "usage_log",
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at            TEXT    NOT NULL,
            provider              TEXT    NOT NULL,
            client                TEXT    NOT NULL,
            project               TEXT    NOT NULL,
            step                  TEXT    NOT NULL,
            model                 TEXT,
            origin                TEXT,
            input_tokens          INTEGER,
            output_tokens         INTEGER,
            cache_read_tokens     INTEGER,
            cache_creation_tokens INTEGER,
            web_search_requests   INTEGER,
            input_price_per_mtok       REAL,
            output_price_per_mtok      REAL,
            cache_read_price_per_mtok  REAL,
            cache_write_price_per_mtok REAL,
            cost_usd              REAL,
            session_id            TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_created_at ON usage_log(created_at)")
    conn.commit()
    return conn


def _insertar(fila: dict) -> None:
    fila = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fila}
    columnas = ", ".join(fila)
    marcas = ", ".join("?" * len(fila))
    with _conn() as conn:
        conn.execute(f"INSERT INTO usage_log ({columnas}) VALUES ({marcas})", tuple(fila.values()))


def record(
    client: str, project: str | None, step: str, payload: Any, origin: str | None = None
) -> None:
    """
    Apunta el consumo de una llamada a un modelo de IA.

    client:  nombre registrado en provider_factory ('anthropic_api', 'claude_code').
             Decide como se lee el payload y quien cobra (UsageProvider.vendor).
    payload: la respuesta tal cual la devolvio el cliente.
    """
    try:
        lector = provider_factory.get_usage_provider(client)
        for registro in lector.parse(payload):
            _insertar({"provider": lector.vendor, "client": client,
                       "project": project or SIN_PROYECTO,
                       "step": step, "origin": origin, **registro})
    except Exception:
        logger.exception(
            "usage_log: no se pudo registrar el consumo (%s/%s/%s)", client, project, step
        )


def project_of(lookup, *args) -> str:
    """
    Proyecto al que imputar un gasto, sin poder romper nada.

    `lookup` es la funcion que lo averigua (p. ej. project_map.get_project_label). Si
    falla, el gasto se apunta como SIN_PROYECTO: averiguar a quien imputarlo nunca
    puede tumbar la respuesta al usuario ni el resumen del historial.
    """
    try:
        return lookup(*args) or SIN_PROYECTO
    except Exception:
        logger.exception("usage_log: no se pudo averiguar el proyecto; se imputa a %s", SIN_PROYECTO)
        return SIN_PROYECTO


def _inicio_utc(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _filtro(desde: str, project: str | None) -> tuple[str, tuple]:
    if project is None:
        return "created_at >= ?", (desde,)
    return "created_at >= ? AND project = ?", (desde, project)


def _agrupado(conn: sqlite3.Connection, columna: str, desde: str, project: str | None = None) -> dict:
    where, args = _filtro(desde, project)
    filas = conn.execute(
        f"SELECT {columna}, COALESCE(SUM(cost_usd), 0) FROM usage_log "
        f"WHERE {where} GROUP BY {columna}",
        args,
    ).fetchall()
    return {clave: round(coste, 4) for clave, coste in filas}


def list_projects() -> list[str]:
    """Proyectos que tienen algun gasto registrado, para poder preguntar cual se quiere."""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM usage_log ORDER BY project"
        )]


def resolve_project(nombre: str) -> str | None:
    """Proyecto registrado que coincide con `nombre` (sin distinguir mayusculas), o None."""
    buscado = (nombre or "").strip().lower()
    return next((p for p in list_projects() if p.lower() == buscado), None)


def _por_modelo(conn: sqlite3.Connection, desde: str, project: str | None = None) -> list[dict]:
    """
    Tokens y coste acumulados por proveedor y modelo, con el rango de precios que se
    aplico en el periodo y el precio vigente hoy en model_prices.
    """
    filas = conn.execute(
        """
        SELECT provider, model, COUNT(*),
               COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
               COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cache_creation_tokens), 0),
               COALESCE(SUM(cost_usd), 0)
        FROM usage_log WHERE {where}
        GROUP BY provider, model ORDER BY SUM(cost_usd) DESC
        """.format(where=_filtro(desde, project)[0]),
        _filtro(desde, project)[1],
    ).fetchall()
    resultado = []
    for provider, model, n, inp, out, cread, cwrite, coste in filas:
        aplicados = conn.execute(
            """
            SELECT MIN(input_price_per_mtok), MAX(input_price_per_mtok),
                   MIN(output_price_per_mtok), MAX(output_price_per_mtok),
                   MIN(cache_read_price_per_mtok), MAX(cache_read_price_per_mtok),
                   MIN(cache_write_price_per_mtok), MAX(cache_write_price_per_mtok)
            FROM usage_log
            WHERE {where} AND provider = ? AND model IS ?
            """.format(where=_filtro(desde, project)[0]),
            (*_filtro(desde, project)[1], provider, model),
        ).fetchone()
        precio = model_prices.get_price(provider, model)
        resultado.append({
            "provider": provider,
            "model": model,
            "llamadas": n,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cread,
            "cache_creation_tokens": cwrite,
            "cost_usd": round(coste, 4),
            # Lo que se cobro: si el precio cambio en el periodo, min y max difieren.
            "precio_aplicado_por_mtok": (
                {"input_min": aplicados[0], "input_max": aplicados[1],
                 "output_min": aplicados[2], "output_max": aplicados[3],
                 "cache_read_min": aplicados[4], "cache_read_max": aplicados[5],
                 "cache_write_min": aplicados[6], "cache_write_max": aplicados[7]}
                if aplicados[0] is not None else None
            ),
            # El de hoy en model_prices, para comparar.
            "precio_actual_por_mtok": (
                {"input": precio["input_per_mtok"], "output": precio["output_per_mtok"],
                 "cache_read": precio["cache_read_per_mtok"],
                 "cache_write": precio["cache_write_per_mtok"],
                 "checked_at": precio["checked_at"]} if precio else None
            ),
        })
    return resultado


def _totales(conn: sqlite3.Connection, desde: str, project: str | None = None) -> dict:
    where, args = _filtro(desde, project)
    llamadas, sin_coste = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(cost_usd IS NULL), 0) FROM usage_log WHERE {where}",
        args,
    ).fetchone()
    por_proyecto = _agrupado(conn, "project", desde, project)
    return {
        "total_usd": round(sum(por_proyecto.values()), 4),
        "por_proyecto": por_proyecto,
        "por_proveedor": _agrupado(conn, "provider", desde, project),
        "por_modelo": _por_modelo(conn, desde, project),
        "llamadas": llamadas,
        "registros_sin_coste": sin_coste,
    }


def get_summary(now: datetime | None = None, project: str | None = None) -> dict:
    """
    Gasto de hoy y del mes en curso (zona horaria de config.timezone()), acumulado y
    desglosado por proyecto, por proveedor y por modelo (con tokens y precio).
    Con `project`, solo el gasto de ese proyecto (nombre exacto; ver resolve_project).
    'registros_sin_coste' > 0 significa que el total esta por debajo de la realidad.
    """
    ahora = (now or datetime.now(timezone.utc)).astimezone(config.timezone())
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    mes = hoy.replace(day=1)
    with _conn() as conn:
        return {
            "hoy": _totales(conn, _inicio_utc(hoy), project),
            "mes": _totales(conn, _inicio_utc(mes), project),
        }
