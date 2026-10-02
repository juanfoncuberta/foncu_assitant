"""
Configuracion compartida del sistema: el UNICO sitio donde viven los valores por
defecto. Los modulos no repiten literales (rutas, nombres de base de datos, URLs):
llaman a estas funciones, que leen el entorno en cada llamada.

Leer en cada llamada, y no al importar, es lo que permite cambiar la configuracion
sin tocar codigo (y en los tests, redirigirla con monkeypatch.setenv).
"""

import os
from zoneinfo import ZoneInfo

_DEFAULT_DB_PATH = "assistant.db"
_DEFAULT_TIMEZONE = "Europe/Madrid"
_DEFAULT_MODEL_PRICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_prices.json")


def db_path() -> str:
    """Ruta de la base de datos SQLite. Se sobrescribe con DB_PATH en el entorno."""
    return os.environ.get("DB_PATH", "").strip() or _DEFAULT_DB_PATH


def model_prices_file() -> str:
    """Archivo de datos con los precios iniciales de modelos. Se sobrescribe con MODEL_PRICES_FILE."""
    return os.environ.get("MODEL_PRICES_FILE", "").strip() or _DEFAULT_MODEL_PRICES_FILE


def timezone() -> ZoneInfo:
    """Zona horaria local del usuario (cortes de dia y mes). Se sobrescribe con TIMEZONE."""
    return ZoneInfo(os.environ.get("TIMEZONE", "").strip() or _DEFAULT_TIMEZONE)
