import pytest

import project_directory_map
import project_map


@pytest.fixture(autouse=True)
def _base_de_datos_temporal_para_config(tmp_path, monkeypatch):
    """
    Los modulos que piden la ruta a config.db_path() (hoy usage_log y model_prices)
    la leen del entorno en cada llamada, asi que basta con redirigir DB_PATH.

    Sin esto, cualquier test que ejercite el executor, el bot o el resumen del
    historial apuntaria gasto falso en el assistant.db real; y en el droplet DB_PATH
    apunta a la base de produccion.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_config.db"))
    # project_map y project_directory_map aun leen DB_PATH al importarse (pendiente de
    # migrar a config). El bot y el resumen del historial los consultan para saber el
    # proyecto de cada gasto: sin esto, esos tests leerian el assistant.db real.
    monkeypatch.setattr(project_map, "DB_PATH", str(tmp_path / "test_project_map.db"))
    monkeypatch.setattr(project_directory_map, "DB_PATH", str(tmp_path / "test_project_map.db"))


# Precios INVENTADOS, distintos de los reales a proposito: si un test pasara solo
# porque coincide con model_prices.json, esto lo destaparia.
PRECIOS_DE_PRUEBA = {
    "claude-sonnet-4-6": dict(input_per_mtok=2.0, output_per_mtok=10.0,
                              cache_write_per_mtok=2.5, cache_read_per_mtok=0.2,
                              web_search_per_1k=5.0),
    "claude-haiku-4-5": dict(input_per_mtok=0.5, output_per_mtok=4.0,
                             cache_write_per_mtok=0.6, cache_read_per_mtok=0.05,
                             web_search_per_1k=5.0),
}


@pytest.fixture
def precios_de_prueba():
    """Siembra PRECIOS_DE_PRUEBA en la base de datos temporal del test."""
    import model_prices

    for modelo, tarifas in PRECIOS_DE_PRUEBA.items():
        model_prices.upsert_price("anthropic", modelo, checked_at="2026-01-01", **tarifas)
    return PRECIOS_DE_PRUEBA
