from types import SimpleNamespace

import pytest

from anthropic_usage_provider import (
    AnthropicApiUsageProvider,
    ClaudeCodeUsageProvider,
    estimate_cost,
)

# Salida literal de `claude -p "di hola" --output-format json` (28/09/2026), recortada.
CLI_OUTPUT = {
    "type": "result",
    "result": "hola",
    "session_id": "sess-123",
    "total_cost_usd": 0.0207,
    "usage": {
        "input_tokens": 4,
        "output_tokens": 12,
        "cache_read_input_tokens": 16472,
        "cache_creation_input_tokens": 0,
    },
    # Desglose por modelo de la CLI (campos en camelCase). Las cifras de tokens por
    # modelo son ilustrativas: el formato de los campos se verifica en el primer
    # uso real (si no vinieran, parse cae a la fila agregada y el coste sigue bien).
    "modelUsage": {
        "claude-opus-4-8": {
            "inputTokens": 4,
            "outputTokens": 12,
            "cacheReadInputTokens": 16472,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
            "costUSD": 0.0207,
        }
    },
}


def respuesta_api(model="claude-sonnet-4-6", inp=1000, out=500, searches=0):
    """Imita lo que devuelve anthropic.messages.create()."""
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None,
        ),
    )


@pytest.mark.usefixtures("precios_de_prueba")
class TestEstimateCost:
    # Precios inventados de conftest.PRECIOS_DE_PRUEBA: sonnet 2/10, cache 2.5/0.2,
    # busqueda 5 por 1000; haiku 0.5/4.

    def test_entrada_y_salida(self):
        assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == 12.0

    def test_haiku_con_sufijo_de_fecha(self):
        # El id que usa conversation_memory lleva fecha: tiene que encontrar la tarifa.
        assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == 0.5

    def test_cache_se_cobra_a_su_tarifa(self):
        coste = estimate_cost(
            "claude-sonnet-4-6", cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000
        )
        assert coste == pytest.approx(0.2 + 2.5)

    def test_busquedas_web(self):
        assert estimate_cost("claude-sonnet-4-6", web_search_requests=4) == 0.02

    def test_modelo_sin_precio_devuelve_none(self):
        assert estimate_cost("claude-opus-9", 1000, 1000) is None

    def test_modelo_que_no_es_texto_no_tiene_precio(self):
        assert estimate_cost(object(), 1000) is None

    def test_tokens_none_cuentan_como_cero(self):
        assert estimate_cost("claude-sonnet-4-6", None, None) == 0.0

    def test_usa_el_precio_actual_de_la_base_de_datos(self):
        # Cambiar el precio en la tabla cambia el calculo, sin tocar codigo.
        import model_prices
        model_prices.upsert_price("anthropic", "claude-sonnet-4-6", input_per_mtok=100.0,
                                  output_per_mtok=0.0, checked_at="2026-12-01")
        assert estimate_cost("claude-sonnet-4-6", 1_000_000, 0) == 100.0


def test_sin_precios_en_la_base_de_datos_no_hay_coste():
    # La tabla vacia (sin sembrar) demuestra que no queda ningun precio en el codigo.
    assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) is None


@pytest.mark.usefixtures("precios_de_prueba")
class TestAnthropicApiUsageProvider:
    def test_una_fila_con_tokens_coste_y_precio_aplicado(self):
        [r] = AnthropicApiUsageProvider().parse(respuesta_api(inp=1000, out=500))
        assert r["model"] == "claude-sonnet-4-6"
        assert (r["input_tokens"], r["output_tokens"]) == (1000, 500)
        assert r["cost_usd"] == pytest.approx((1000 * 2 + 500 * 10) / 1_000_000)
        assert r["session_id"] is None
        assert (r["input_price_per_mtok"], r["output_price_per_mtok"]) == (2.0, 10.0)
        assert (r["cache_read_price_per_mtok"], r["cache_write_price_per_mtok"]) == (0.2, 2.5)

    def test_busquedas_web(self):
        [r] = AnthropicApiUsageProvider().parse(respuesta_api(searches=2))
        assert r["web_search_requests"] == 2

    def test_modelo_desconocido_sin_coste_ni_precio_y_avisa(self, caplog):
        [r] = AnthropicApiUsageProvider().parse(respuesta_api(model="claude-opus-9"))
        assert r["cost_usd"] is None
        assert r["input_price_per_mtok"] is None
        assert "claude-opus-9" in caplog.text
        assert "actualizar_precio_ia" in caplog.text

    def test_payload_sin_usage_lanza(self):
        # usage_log captura la excepcion; el parser no tiene por que tragarsela.
        with pytest.raises(AttributeError):
            AnthropicApiUsageProvider().parse(object())


class TestClaudeCodeUsageProvider:
    def test_una_fila_por_modelo_con_sus_tokens_y_su_coste(self):
        [r] = ClaudeCodeUsageProvider().parse(CLI_OUTPUT)
        assert r["model"] == "claude-opus-4-8"
        assert r["cost_usd"] == 0.0207
        assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"]) == (4, 12, 16472)
        assert r["session_id"] == "sess-123"

    def test_varios_modelos_dan_varias_filas_que_suman_el_total(self, precios_de_prueba):
        datos = {**CLI_OUTPUT, "total_cost_usd": 0.5, "modelUsage": {
            "claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50, "costUSD": 0.4},
            "claude-haiku-4-5-20251001": {"inputTokens": 30, "outputTokens": 10, "costUSD": 0.1},
        }}

        filas = ClaudeCodeUsageProvider().parse(datos)

        assert [f["model"] for f in filas] == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
        assert sum(f["cost_usd"] for f in filas) == pytest.approx(0.5)
        haiku, sonnet = filas
        # Cada modelo con su precio por token aplicado (inventado en conftest).
        assert (sonnet["input_price_per_mtok"], sonnet["output_price_per_mtok"]) == (2.0, 10.0)
        assert haiku["input_price_per_mtok"] == 0.5
        assert (sonnet["input_tokens"], haiku["input_tokens"]) == (100, 30)

    def test_sin_coste_por_modelo_cae_a_una_fila_agregada(self):
        datos = {**CLI_OUTPUT, "modelUsage": {"claude-opus-4-8": {"inputTokens": 4}}}
        [r] = ClaudeCodeUsageProvider().parse(datos)
        assert r["cost_usd"] == 0.0207  # el total de la sesion, de total_cost_usd
        assert r["input_tokens"] == 4   # de usage, no del desglose

    def test_agregado_con_varios_modelos_no_atribuye_precio(self, precios_de_prueba):
        datos = {**CLI_OUTPUT, "modelUsage": {"claude-sonnet-4-6": {}, "claude-haiku-4-5": {}}}
        [r] = ClaudeCodeUsageProvider().parse(datos)
        assert r["model"] == "claude-haiku-4-5,claude-sonnet-4-6"
        assert r["input_price_per_mtok"] is None

    def test_agregado_con_un_solo_modelo_atribuye_precio(self, precios_de_prueba):
        datos = {**CLI_OUTPUT, "modelUsage": {"claude-sonnet-4-6": {}}}
        [r] = ClaudeCodeUsageProvider().parse(datos)
        assert (r["input_price_per_mtok"], r["output_price_per_mtok"]) == (2.0, 10.0)

    def test_acepta_el_nombre_antiguo_cost_usd(self):
        datos = {k: v for k, v in CLI_OUTPUT.items() if k not in ("total_cost_usd", "modelUsage")}
        [r] = ClaudeCodeUsageProvider().parse({**datos, "cost_usd": 0.5})
        assert r["cost_usd"] == 0.5

    def test_salida_minima(self):
        [r] = ClaudeCodeUsageProvider().parse({"result": "x"})
        assert r["cost_usd"] is None
        assert r["input_tokens"] is None
        assert r["model"] is None


def test_ambos_devuelven_las_mismas_claves():
    # usage_log inserta el registro tal cual: si un proveedor devolviera otra
    # clave, el INSERT fallaria y ese gasto desapareceria del total.
    [api] = AnthropicApiUsageProvider().parse(respuesta_api())
    [cli] = ClaudeCodeUsageProvider().parse(CLI_OUTPUT)
    [agregado] = ClaudeCodeUsageProvider().parse({"result": "x"})
    assert set(api) == set(cli) == set(agregado)
