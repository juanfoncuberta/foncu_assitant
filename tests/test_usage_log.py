import sqlite3
from datetime import datetime, timezone

import pytest

import config
import provider_factory
import usage_log
from tests.test_anthropic_usage_provider import CLI_OUTPUT, respuesta_api


def _filas():
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_log ORDER BY id")]
    except sqlite3.OperationalError:
        return []


@pytest.mark.usefixtures("precios_de_prueba")
class TestRecord:
    def test_guarda_proveedor_proyecto_paso_y_lo_que_devuelve_el_parser(self):
        usage_log.record("anthropic_api", "foncu_assitant", "chat", respuesta_api(), origin="chat:1/None")

        [fila] = _filas()
        assert (fila["provider"], fila["client"]) == ("anthropic", "anthropic_api")
        assert (fila["project"], fila["step"]) == ("foncu_assitant", "chat")
        # Precio por token aplicado (precios inventados de conftest).
        assert (fila["input_price_per_mtok"], fila["output_price_per_mtok"]) == (2.0, 10.0)
        assert fila["origin"] == "chat:1/None"
        assert fila["model"] == "claude-sonnet-4-6"
        assert fila["cost_usd"] == pytest.approx((1000 * 2 + 500 * 10) / 1_000_000)

    def test_usa_el_proveedor_que_le_digan(self):
        usage_log.record("claude_code", "linkedin", "exec", CLI_OUTPUT, origin="feat/x-1")
        [fila] = _filas()
        # Lo cobra el mismo que la API: el proveedor es quien cobra, no la via.
        assert (fila["provider"], fila["client"]) == ("anthropic", "claude_code")
        assert fila["cost_usd"] == 0.0207

    def test_un_proveedor_nuevo_solo_necesita_registrarse_en_la_factory(self, monkeypatch):
        class Falso(provider_factory.UsageProvider):
            name = "otro_api"
            vendor = "otro"

            def parse(self, payload):
                return [{"model": "m", "input_tokens": 1, "output_tokens": 1,
                         "cache_read_tokens": 0, "cache_creation_tokens": 0,
                         "web_search_requests": 0, "input_price_per_mtok": None,
                         "output_price_per_mtok": None, "cache_read_price_per_mtok": None,
                         "cache_write_price_per_mtok": None, "cost_usd": 0.5,
                         "session_id": None}]

        registro_real = provider_factory._usage_providers
        monkeypatch.setattr(provider_factory, "_usage_providers",
                            lambda: {**registro_real(), "otro_api": Falso})

        usage_log.record("otro_api", "foncu_assitant", "chat", object())

        assert (_filas()[0]["provider"], _filas()[0]["client"]) == ("otro", "otro_api")
        assert usage_log.get_summary()["hoy"]["por_proveedor"] == {"otro": 0.5}

    def test_proveedor_desconocido_no_lanza(self, caplog):
        usage_log.record("openai", "foncu_assitant", "chat", object())
        assert "no se pudo registrar" in caplog.text
        assert _filas() == []

    def test_payload_raro_no_lanza(self, caplog):
        usage_log.record("anthropic_api", "foncu_assitant", "chat", object())
        assert "no se pudo registrar" in caplog.text

    def test_base_de_datos_inaccesible_no_lanza(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "no-existe" / "x.db"))
        usage_log.record("anthropic_api", "foncu_assitant", "chat", respuesta_api())
        assert "no se pudo registrar" in caplog.text

    def test_escribe_donde_diga_db_path_en_cada_llamada(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
        usage_log.record("anthropic_api", "foncu_assitant", "chat", respuesta_api())
        monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
        usage_log.record("anthropic_api", "foncu_assitant", "chat", respuesta_api())

        for nombre in ("a.db", "b.db"):
            n = sqlite3.connect(tmp_path / nombre).execute("SELECT COUNT(*) FROM usage_log")
            assert n.fetchone()[0] == 1


def _insertar(created_at: str, project: str, cost, provider="anthropic", model="m",
              inp=0, out=0, precio_in=None, precio_out=None):
    usage_log._insertar({"provider": provider, "client": "x", "project": project,
                         "step": "x", "model": model, "input_tokens": inp,
                         "output_tokens": out, "input_price_per_mtok": precio_in,
                         "output_price_per_mtok": precio_out, "cost_usd": cost})
    with sqlite3.connect(config.db_path()) as conn:
        conn.execute(
            "UPDATE usage_log SET created_at = ? WHERE id = (SELECT MAX(id) FROM usage_log)",
            (created_at,),
        )


def test_la_api_y_claude_code_suman_en_el_mismo_proveedor(precios_de_prueba):
    usage_log.record("anthropic_api", "foncu_assitant", "chat", respuesta_api())
    usage_log.record("claude_code", "linkedin", "exec", CLI_OUTPUT)

    mes = usage_log.get_summary()["mes"]

    assert set(mes["por_proveedor"]) == {"anthropic"}
    assert set(mes["por_proyecto"]) == {"foncu_assitant", "linkedin"}


class TestGetSummary:
    # 15/10/2026 a las 12:00 en Madrid (UTC+2).
    AHORA = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)

    def test_sin_datos(self):
        res = usage_log.get_summary(self.AHORA)
        assert res["hoy"]["total_usd"] == 0
        assert res["mes"]["llamadas"] == 0
        assert res["mes"]["registros_sin_coste"] == 0

    def test_separa_hoy_mes_proyecto_y_proveedor(self):
        _insertar("2026-10-15T08:00:00+00:00", "foncu", 0.10)                   # hoy
        _insertar("2026-10-15T09:00:00+00:00", "linkedin", 1.00, provider="otro")  # hoy
        _insertar("2026-10-03T12:00:00+00:00", "foncu", 0.50)                   # este mes
        _insertar("2026-09-30T12:00:00+00:00", "foncu", 9.99)                   # mes pasado

        res = usage_log.get_summary(self.AHORA)

        assert res["hoy"]["total_usd"] == 1.10
        assert res["hoy"]["por_proyecto"] == {"foncu": 0.10, "linkedin": 1.00}
        assert res["mes"]["total_usd"] == 1.60
        assert res["mes"]["por_proyecto"] == {"foncu": 0.60, "linkedin": 1.00}
        assert res["mes"]["por_proveedor"] == {"anthropic": 0.60, "otro": 1.00}
        assert res["mes"]["llamadas"] == 3

    def test_el_dia_empieza_a_medianoche_de_madrid_no_de_utc(self):
        _insertar("2026-10-14T23:30:00+00:00", "p", 0.20)  # 01:30 del 15 en Madrid
        _insertar("2026-10-14T21:30:00+00:00", "p", 0.40)  # 23:30 del 14 en Madrid
        assert usage_log.get_summary(self.AHORA)["hoy"]["total_usd"] == 0.20

    def test_los_cortes_siguen_la_zona_horaria_de_config(self, monkeypatch):
        # En UTC, las 23:30 UTC del 14/10 son "ayer" (en Madrid serian "hoy").
        monkeypatch.setenv("TIMEZONE", "UTC")
        _insertar("2026-10-14T23:30:00+00:00", "p", 0.20)
        assert usage_log.get_summary(self.AHORA)["hoy"]["total_usd"] == 0

    def test_el_mes_empieza_a_medianoche_de_madrid(self):
        _insertar("2026-09-30T22:30:00+00:00", "p", 0.70)  # 00:30 del 1/10 en Madrid
        assert usage_log.get_summary(self.AHORA)["mes"]["total_usd"] == 0.70

    def test_cuenta_los_registros_sin_coste(self):
        _insertar("2026-10-15T08:00:00+00:00", "p", None)
        _insertar("2026-10-15T08:00:00+00:00", "p", 0.10)

        mes = usage_log.get_summary(self.AHORA)["mes"]

        assert mes["registros_sin_coste"] == 1
        assert mes["total_usd"] == 0.10
        assert mes["llamadas"] == 2


class TestProyectoYModelo:
    AHORA = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)

    def test_sin_proyecto_se_apunta_como_sin_proyecto(self, precios_de_prueba):
        usage_log.record("anthropic_api", None, "chat", respuesta_api())
        assert _filas()[0]["project"] == usage_log.SIN_PROYECTO

    def test_desglose_por_modelo_con_tokens_coste_y_precio_actual(self, precios_de_prueba):
        _insertar("2026-10-15T08:00:00+00:00", "p", 0.30, model="claude-sonnet-4-6", inp=100, out=20)
        _insertar("2026-10-15T08:30:00+00:00", "p", 0.20, model="claude-sonnet-4-6", inp=50, out=10)
        _insertar("2026-10-15T09:00:00+00:00", "p", 0.05, model="claude-haiku-4-5-20251001", inp=10, out=5)

        por_modelo = usage_log.get_summary(self.AHORA)["hoy"]["por_modelo"]

        sonnet, haiku = por_modelo  # ordenado de mas a menos gasto
        assert sonnet["model"] == "claude-sonnet-4-6"
        assert (sonnet["llamadas"], sonnet["input_tokens"], sonnet["output_tokens"]) == (2, 150, 30)
        assert sonnet["cost_usd"] == 0.5
        assert sonnet["precio_actual_por_mtok"] == {
            "input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5,
            "checked_at": "2026-01-01",
        }
        # Id con fecha: encuentra el precio de la fila base.
        assert haiku["precio_actual_por_mtok"]["input"] == 0.5

    def test_modelo_sin_precio_sale_con_precio_none(self):
        _insertar("2026-10-15T08:00:00+00:00", "p", None, model="desconocido")
        [fila] = usage_log.get_summary(self.AHORA)["hoy"]["por_modelo"]
        assert fila["precio_actual_por_mtok"] is None


def test_una_sesion_con_varios_modelos_guarda_una_fila_por_modelo(precios_de_prueba):
    datos = {**CLI_OUTPUT, "modelUsage": {
        "claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50, "costUSD": 0.4},
        "claude-haiku-4-5": {"inputTokens": 30, "outputTokens": 10, "costUSD": 0.1},
    }}
    usage_log.record("claude_code", "foncu_assitant", "exec", datos, origin="feat/x-1")

    filas = _filas()

    assert sorted(f["model"] for f in filas) == ["claude-haiku-4-5", "claude-sonnet-4-6"]
    assert {f["origin"] for f in filas} == {"feat/x-1"}
    assert usage_log.get_summary()["hoy"]["total_usd"] == 0.5


def test_el_desglose_ensena_el_precio_aplicado_aunque_haya_cambiado(precios_de_prueba):
    ahora = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    _insertar("2026-10-02T08:00:00+00:00", "p", 0.1, model="claude-sonnet-4-6", precio_in=3.0, precio_out=15.0)
    _insertar("2026-10-14T08:00:00+00:00", "p", 0.1, model="claude-sonnet-4-6", precio_in=4.0, precio_out=20.0)

    [fila] = usage_log.get_summary(ahora)["mes"]["por_modelo"]

    aplicado = fila["precio_aplicado_por_mtok"]
    assert (aplicado["input_min"], aplicado["input_max"]) == (3.0, 4.0)
    assert (aplicado["output_min"], aplicado["output_max"]) == (15.0, 20.0)
    # Y el de hoy en model_prices (inventado en conftest), para comparar.
    assert fila["precio_actual_por_mtok"]["input"] == 2.0


def test_sin_precio_aplicado_conocido_sale_none():
    ahora = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    _insertar("2026-10-14T08:00:00+00:00", "p", None, model="x")
    [fila] = usage_log.get_summary(ahora)["mes"]["por_modelo"]
    assert fila["precio_aplicado_por_mtok"] is None


class TestProjectOf:
    def test_devuelve_lo_que_da_la_busqueda(self):
        assert usage_log.project_of(lambda a, b: "Cuoco", 1, 2) == "Cuoco"

    def test_sin_resultado_es_sin_proyecto(self):
        assert usage_log.project_of(lambda: None) == usage_log.SIN_PROYECTO

    def test_si_la_busqueda_falla_no_lanza_y_es_sin_proyecto(self, caplog):
        def rota():
            raise RuntimeError("no such column: chat_id")
        assert usage_log.project_of(rota) == usage_log.SIN_PROYECTO
        assert "no se pudo averiguar el proyecto" in caplog.text


def test_el_desglose_incluye_las_tarifas_de_cache(precios_de_prueba):
    # Precios inventados de conftest: sonnet cache_read 0.2, cache_write 2.5.
    usage_log.record("anthropic_api", "p", "chat", respuesta_api())

    [fila] = usage_log.get_summary()["hoy"]["por_modelo"]

    aplicado = fila["precio_aplicado_por_mtok"]
    assert (aplicado["cache_read_min"], aplicado["cache_read_max"]) == (0.2, 0.2)
    assert (aplicado["cache_write_min"], aplicado["cache_write_max"]) == (2.5, 2.5)
    actual = fila["precio_actual_por_mtok"]
    assert (actual["cache_read"], actual["cache_write"]) == (0.2, 2.5)


def test_una_tabla_usage_log_antigua_se_aparta_y_no_se_pierde_nada(caplog):
    import sqlite3 as _sq
    with _sq.connect(config.db_path()) as conn:
        conn.execute("CREATE TABLE usage_log (id INTEGER PRIMARY KEY, source TEXT, cost_usd REAL)")
        conn.execute("CREATE INDEX idx_usage_log_created_at ON usage_log(id)")
        conn.execute("INSERT INTO usage_log (source, cost_usd) VALUES ('bot', 1.5)")

    usage_log.record("claude_code", "p", "exec", CLI_OUTPUT)

    assert len(_filas()) == 1  # el registro nuevo entra en la tabla nueva
    with _sq.connect(config.db_path()) as conn:
        antiguas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'usage_log_old_%'")]
        assert len(antiguas) == 1
        assert conn.execute(f"SELECT cost_usd FROM {antiguas[0]}").fetchone()[0] == 1.5
        indices = {r[1] for r in conn.execute("PRAGMA index_list(usage_log)")}
    assert "idx_usage_log_created_at" in indices  # la tabla nueva conserva su indice
    assert "Se ha renombrado" in caplog.text


def test_resumen_filtrado_por_proyecto(precios_de_prueba):
    usage_log.record("anthropic_api", "a", "chat", respuesta_api())
    usage_log.record("claude_code", "b", "exec", CLI_OUTPUT)

    solo_b = usage_log.get_summary(project="b")["mes"]

    assert set(solo_b["por_proyecto"]) == {"b"}
    assert solo_b["llamadas"] == 1
    assert [m["model"] for m in solo_b["por_modelo"]] == ["claude-opus-4-8"]


def test_resolve_project_sin_distinguir_mayusculas_y_sin_adivinar(precios_de_prueba):
    usage_log.record("anthropic_api", "Cuoco", "chat", respuesta_api())
    assert usage_log.resolve_project(" cuoco ") == "Cuoco"
    assert usage_log.resolve_project("cuo") is None
    assert usage_log.list_projects() == ["Cuoco"]
