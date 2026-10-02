import json
from pathlib import Path

import pytest

import config
import model_prices

REPO = Path(__file__).resolve().parent.parent


def _precio(**extra):
    base = dict(provider="anthropic", model="m-1", input_per_mtok=1.0,
                output_per_mtok=2.0, checked_at="2026-10-02")
    return {**base, **extra}


class TestUpsertYGet:
    def test_guarda_y_recupera(self):
        model_prices.upsert_price(**_precio(cache_read_per_mtok=0.1, source_url="https://x"))
        p = model_prices.get_price("anthropic", "m-1")
        assert (p["input_per_mtok"], p["output_per_mtok"], p["cache_read_per_mtok"]) == (1.0, 2.0, 0.1)
        assert p["checked_at"] == "2026-10-02"
        assert p["source_url"] == "https://x"

    def test_reemplaza_el_precio_existente(self):
        model_prices.upsert_price(**_precio())
        model_prices.upsert_price(**_precio(input_per_mtok=9.0, checked_at="2026-11-01"))
        assert len(model_prices.list_prices()) == 1
        p = model_prices.get_price("anthropic", "m-1")
        assert (p["input_per_mtok"], p["checked_at"]) == (9.0, "2026-11-01")

    def test_id_con_sufijo_de_fecha_usa_la_fila_base(self):
        model_prices.upsert_price(**_precio(model="claude-haiku-4-5"))
        assert model_prices.get_price("anthropic", "claude-haiku-4-5-20251001")["model"] == "claude-haiku-4-5"

    def test_gana_el_prefijo_mas_largo(self):
        model_prices.upsert_price(**_precio(model="m", input_per_mtok=1.0))
        model_prices.upsert_price(**_precio(model="m-pro", input_per_mtok=5.0))
        assert model_prices.get_price("anthropic", "m-pro-2026")["input_per_mtok"] == 5.0

    def test_prefijo_sin_guion_no_cuenta(self):
        model_prices.upsert_price(**_precio(model="claude-sonnet-4-6"))
        assert model_prices.get_price("anthropic", "claude-sonnet-4-60") is None

    def test_otro_proveedor_no_cuenta(self):
        model_prices.upsert_price(**_precio())
        assert model_prices.get_price("openai", "m-1") is None

    @pytest.mark.parametrize("modelo", [None, "", 123])
    def test_modelo_invalido_no_tiene_precio(self, modelo):
        assert model_prices.get_price("anthropic", modelo) is None

    def test_list_filtra_por_proveedor(self):
        model_prices.upsert_price(**_precio())
        model_prices.upsert_price(**_precio(provider="otro"))
        assert [p["provider"] for p in model_prices.list_prices("otro")] == ["otro"]
        assert len(model_prices.list_prices()) == 2


class TestValidacion:
    @pytest.mark.parametrize("campo,valor", [
        ("input_per_mtok", -1), ("output_per_mtok", "3"), ("cache_read_per_mtok", None),
        ("web_search_per_1k", True),
    ])
    def test_precio_negativo_o_no_numerico(self, campo, valor):
        with pytest.raises(ValueError, match=campo):
            model_prices.upsert_price(**_precio(**{campo: valor}))

    @pytest.mark.parametrize("fecha", ["02/10/2026", "ayer", "", None])
    def test_fecha_de_consulta_invalida(self, fecha):
        with pytest.raises(ValueError, match="checked_at"):
            model_prices.upsert_price(**_precio(checked_at=fecha))

    def test_sin_modelo(self):
        with pytest.raises(ValueError):
            model_prices.upsert_price(**_precio(model=""))

    def test_un_valor_invalido_no_guarda_nada(self):
        with pytest.raises(ValueError):
            model_prices.upsert_price(**_precio(output_per_mtok=-5))
        assert model_prices.list_prices() == []


class TestLoadSeed:
    def _archivo(self, tmp_path, entradas):
        ruta = tmp_path / "precios.json"
        ruta.write_text(json.dumps({"prices": entradas}), encoding="utf-8")
        return str(ruta)

    def test_carga_los_que_faltan(self, tmp_path):
        ruta = self._archivo(tmp_path, [_precio(model="a"), _precio(model="b")])
        assert model_prices.load_seed(ruta) == 2
        assert {p["model"] for p in model_prices.list_prices()} == {"a", "b"}

    def test_no_pisa_un_precio_que_ya_existe(self, tmp_path):
        # Un precio cambiado desde el bot no puede volver al del archivo al reiniciar.
        model_prices.upsert_price(**_precio(model="a", input_per_mtok=7.0))
        ruta = self._archivo(tmp_path, [_precio(model="a", input_per_mtok=1.0), _precio(model="b")])

        assert model_prices.load_seed(ruta) == 1
        assert model_prices.get_price("anthropic", "a")["input_per_mtok"] == 7.0

    def test_es_idempotente(self, tmp_path):
        ruta = self._archivo(tmp_path, [_precio()])
        model_prices.load_seed(ruta)
        assert model_prices.load_seed(ruta) == 0

    def test_archivo_ausente_no_lanza(self, tmp_path, caplog):
        assert model_prices.load_seed(str(tmp_path / "no-existe.json")) == 0
        assert "no-existe.json" in caplog.text

    def test_archivo_roto_no_lanza(self, tmp_path, caplog):
        ruta = tmp_path / "roto.json"
        ruta.write_text("{ no es json", encoding="utf-8")
        assert model_prices.load_seed(str(ruta)) == 0
        assert "No se pudo leer" in caplog.text

    def test_entrada_invalida_se_salta_y_carga_el_resto(self, tmp_path, caplog):
        ruta = self._archivo(tmp_path, [_precio(model="a", input_per_mtok=-1), _precio(model="b")])
        assert model_prices.load_seed(ruta) == 1
        assert [p["model"] for p in model_prices.list_prices()] == ["b"]
        assert "invalida" in caplog.text

    def test_sin_argumento_usa_el_archivo_de_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODEL_PRICES_FILE", self._archivo(tmp_path, [_precio()]))
        assert model_prices.load_seed() == 1


def test_el_archivo_de_datos_del_repo_es_valido():
    """model_prices.json se carga entero: ninguna entrada se salta por invalida."""
    datos = json.loads((REPO / "model_prices.json").read_text(encoding="utf-8"))
    assert datos["prices"], "model_prices.json no tiene precios"
    assert model_prices.load_seed(str(REPO / "model_prices.json")) == len(datos["prices"])


def test_config_apunta_al_archivo_del_repo_por_defecto(monkeypatch):
    monkeypatch.delenv("MODEL_PRICES_FILE", raising=False)
    assert Path(config.model_prices_file()) == REPO / "model_prices.json"


def test_get_exact_price_no_resuelve_sufijos():
    model_prices.upsert_price(**_precio(model="claude-haiku-4-5"))
    assert model_prices.get_exact_price("anthropic", "claude-haiku-4-5")["model"] == "claude-haiku-4-5"
    assert model_prices.get_exact_price("anthropic", "claude-haiku-4-5-20251001") is None



def test_una_tabla_model_prices_antigua_se_aparta(caplog):
    import sqlite3
    with sqlite3.connect(config.db_path()) as conn:
        conn.execute("CREATE TABLE model_prices (model TEXT, price REAL)")
        conn.execute("INSERT INTO model_prices VALUES ('x', 1.0)")

    model_prices.upsert_price(**_precio())

    assert model_prices.get_price("anthropic", "m-1") is not None
    assert "Se ha renombrado" in caplog.text



def test_load_seed_nunca_lanza_aunque_falle_la_base_de_datos(tmp_path, monkeypatch, caplog):
    import sqlite3
    ruta = tmp_path / "p.json"
    ruta.write_text(json.dumps({"prices": [_precio()]}), encoding="utf-8")

    def rota(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(model_prices, "list_prices", rota)

    assert model_prices.load_seed(str(ruta)) == 0
    assert "No se pudieron sembrar" in caplog.text
