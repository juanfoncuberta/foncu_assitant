"""
Precios de los modelos de IA, guardados en la base de datos.

Los precios y los modelos cambian con el tiempo, asi que no viven en el codigo:
- La tabla model_prices es la fuente de verdad.
- model_prices.json (ver config.model_prices_file) siembra la tabla al arrancar,
  pero SOLO los (provider, model) que aun no existen: nunca pisa un precio que se
  haya actualizado despues desde el bot.
- La tool actualizar_precio_ia cambia o anade un precio sin tocar codigo.

Un modelo sin fila aqui no tiene precio: su gasto se registra con coste NULL y el
resumen lo avisa, en vez de inventarse un numero.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

import config
import db_schema

logger = logging.getLogger(__name__)

PRICE_FIELDS = (
    "input_per_mtok",
    "output_per_mtok",
    "cache_write_per_mtok",
    "cache_read_per_mtok",
    "web_search_per_1k",
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    db_schema.ensure_table(
        conn,
        "model_prices",
        """
        CREATE TABLE IF NOT EXISTS model_prices (
            provider             TEXT NOT NULL,
            model                TEXT NOT NULL,
            input_per_mtok       REAL NOT NULL,
            output_per_mtok      REAL NOT NULL,
            cache_write_per_mtok REAL NOT NULL DEFAULT 0,
            cache_read_per_mtok  REAL NOT NULL DEFAULT 0,
            web_search_per_1k    REAL NOT NULL DEFAULT 0,
            checked_at           TEXT NOT NULL,
            source_url           TEXT,
            updated_at           TEXT NOT NULL,
            PRIMARY KEY (provider, model)
        )
        """
    )
    conn.commit()
    return conn


def _fila(row: sqlite3.Row) -> dict:
    return dict(row)


def list_prices(provider: str | None = None) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        if provider:
            rows = conn.execute(
                "SELECT * FROM model_prices WHERE provider = ? ORDER BY model", (provider,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM model_prices ORDER BY provider, model").fetchall()
    return [_fila(r) for r in rows]


def get_exact_price(provider: str, model: str) -> dict | None:
    """Fila exacta de (provider, model), sin resolver sufijos de fecha."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM model_prices WHERE provider = ? AND model = ?", (provider, model)
        ).fetchone()
    return _fila(row) if row else None


def get_price(provider: str, model: str | None) -> dict | None:
    """
    Tarifa de un modelo, o None si no esta registrada.

    Acepta ids con sufijo de fecha: 'claude-haiku-4-5-20251001' usa la fila
    'claude-haiku-4-5'. Si hay coincidencia exacta, gana; si no, el prefijo mas largo.
    """
    if not isinstance(model, str) or not model:
        return None
    candidatas = [p for p in list_prices(provider)
                  if model == p["model"] or model.startswith(p["model"] + "-")]
    if not candidatas:
        return None
    return max(candidatas, key=lambda p: len(p["model"]))


def upsert_price(
    provider: str,
    model: str,
    input_per_mtok: float,
    output_per_mtok: float,
    checked_at: str,
    cache_write_per_mtok: float = 0.0,
    cache_read_per_mtok: float = 0.0,
    web_search_per_1k: float = 0.0,
    source_url: str | None = None,
) -> dict:
    """Crea o reemplaza el precio de un modelo. Devuelve la fila guardada."""
    if not provider or not model:
        raise ValueError("provider y model son obligatorios.")
    valores = {
        "input_per_mtok": input_per_mtok,
        "output_per_mtok": output_per_mtok,
        "cache_write_per_mtok": cache_write_per_mtok,
        "cache_read_per_mtok": cache_read_per_mtok,
        "web_search_per_1k": web_search_per_1k,
    }
    for campo, valor in valores.items():
        if not isinstance(valor, (int, float)) or isinstance(valor, bool) or valor < 0:
            raise ValueError(f"{campo} debe ser un numero >= 0 (recibido {valor!r}).")
    try:
        datetime.strptime(checked_at, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"checked_at debe ser una fecha YYYY-MM-DD (recibido {checked_at!r}).")

    fila = {
        "provider": provider,
        "model": model,
        **{k: float(v) for k, v in valores.items()},
        "checked_at": checked_at,
        "source_url": source_url,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    columnas = ", ".join(fila)
    marcas = ", ".join("?" * len(fila))
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO model_prices ({columnas}) VALUES ({marcas})",
            tuple(fila.values()),
        )
    return fila


def load_seed(path: str | None = None) -> int:
    """
    Carga en la tabla los precios del archivo de datos que aun no existan.
    Devuelve cuantos ha anadido. Nunca lanza: un archivo ausente o roto se avisa
    por log y el bot sigue con lo que ya haya en la tabla.
    """
    path = path or config.model_prices_file()
    try:
        return _load_seed(path)
    except Exception:
        # Arrancar sin precios es mejor que no arrancar: el gasto se registrara sin
        # coste y el resumen lo avisara.
        logger.exception("No se pudieron sembrar los precios desde %s.", path)
        return 0


def _load_seed(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            datos = json.load(f)
        entradas = datos["prices"]
    except FileNotFoundError:
        logger.warning("No existe %s: model_prices se queda como esta.", path)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("No se pudo leer %s: model_prices se queda como esta.", path)
        return 0

    existentes = {(p["provider"], p["model"]) for p in list_prices()}
    anadidos = 0
    for entrada in entradas:
        clave = (entrada.get("provider"), entrada.get("model"))
        if clave in existentes:
            continue
        try:
            upsert_price(**entrada)
            anadidos += 1
        except (TypeError, ValueError):
            logger.exception("Entrada de precio invalida en %s: %r", path, entrada)
    if anadidos:
        logger.info("model_prices: %d precio(s) cargados desde %s", anadidos, path)
    return anadidos
