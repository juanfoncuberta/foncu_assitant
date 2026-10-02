"""
Consumo de Anthropic, por sus dos vias:

- AnthropicApiUsageProvider: respuestas de `anthropic.messages.create` (el bot).
  La API devuelve tokens pero no coste: se calcula con los precios de la tabla
  model_prices (ver model_prices.py). Aqui no hay ningun precio escrito.
- ClaudeCodeUsageProvider: salida JSON de `claude -p` (tareas de dev).
  La CLI devuelve el coste real en `total_cost_usd`: se usa tal cual.
"""

import logging
from typing import Any

import model_prices
from usage_provider import UsageProvider, UsageRecord

logger = logging.getLogger(__name__)

#: Quien cobra; clave de los precios en model_prices.
ANTHROPIC_VENDOR = "anthropic"


def _precios_aplicados(tarifa: dict | None) -> dict:
    claves = {
        "input_price_per_mtok": "input_per_mtok",
        "output_price_per_mtok": "output_per_mtok",
        "cache_read_price_per_mtok": "cache_read_per_mtok",
        "cache_write_price_per_mtok": "cache_write_per_mtok",
    }
    return {destino: (tarifa[origen] if tarifa else None) for destino, origen in claves.items()}


def estimate_cost(
    model: str | None,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_creation_tokens: int | None = 0,
    web_search_requests: int | None = 0,
    provider: str = ANTHROPIC_VENDOR,
) -> float | None:
    """
    Coste en USD de una llamada a la API con los precios de la tabla model_prices,
    o None si ese modelo no tiene precio registrado.
    """
    tarifa = model_prices.get_price(provider, model)
    if tarifa is None:
        return None
    coste = (
        (input_tokens or 0) * tarifa["input_per_mtok"]
        + (output_tokens or 0) * tarifa["output_per_mtok"]
        + (cache_read_tokens or 0) * tarifa["cache_read_per_mtok"]
        + (cache_creation_tokens or 0) * tarifa["cache_write_per_mtok"]
    ) / 1_000_000
    coste += (web_search_requests or 0) * tarifa["web_search_per_1k"] / 1000
    return round(coste, 6)


class AnthropicApiUsageProvider(UsageProvider):
    name = "anthropic_api"
    vendor = ANTHROPIC_VENDOR

    def parse(self, payload: Any) -> list[UsageRecord]:
        uso = payload.usage
        modelo = getattr(payload, "model", None)
        server = getattr(uso, "server_tool_use", None)
        tokens = {
            "input_tokens": getattr(uso, "input_tokens", 0) or 0,
            "output_tokens": getattr(uso, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(uso, "cache_read_input_tokens", 0) or 0,
            "cache_creation_tokens": getattr(uso, "cache_creation_input_tokens", 0) or 0,
            "web_search_requests": getattr(server, "web_search_requests", 0) or 0,
        }
        coste = estimate_cost(modelo, provider=self.vendor, **tokens)
        tarifa = model_prices.get_price(self.vendor, modelo)
        if coste is None:
            logger.warning(
                "El modelo %r no tiene precio en model_prices; se guarda sin coste. "
                "Registralo con la tool actualizar_precio_ia.", modelo,
            )
        return [{"model": modelo, **tokens, **_precios_aplicados(tarifa),
                 "cost_usd": coste, "session_id": None}]


class ClaudeCodeUsageProvider(UsageProvider):
    name = "claude_code"
    vendor = ANTHROPIC_VENDOR

    def parse(self, payload: Any) -> list[UsageRecord]:
        """
        Una sesion de `claude -p` puede usar varios modelos (p. ej. uno principal y
        otro auxiliar). La CLI informa de cada uno en `modelUsage.<modelo>` con sus
        tokens y su `costUSD`: se devuelve una fila por modelo. Si ese desglose no
        viene (o no trae coste), se cae a una sola fila con los totales de la sesion.
        """
        por_modelo = self._por_modelo(payload)
        if por_modelo:
            return por_modelo
        return [self._agregado(payload)]

    def _por_modelo(self, payload: dict) -> list[UsageRecord]:
        modelos = payload.get("modelUsage")
        if not isinstance(modelos, dict) or not modelos:
            return []
        if not all(isinstance(d, dict) and d.get("costUSD") is not None for d in modelos.values()):
            return []
        return [
            {
                "model": modelo,
                "input_tokens": datos.get("inputTokens"),
                "output_tokens": datos.get("outputTokens"),
                "cache_read_tokens": datos.get("cacheReadInputTokens"),
                "cache_creation_tokens": datos.get("cacheCreationInputTokens"),
                "web_search_requests": datos.get("webSearchRequests"),
                **_precios_aplicados(model_prices.get_price(self.vendor, modelo)),
                "cost_usd": datos["costUSD"],
                "session_id": payload.get("session_id"),
            }
            for modelo, datos in sorted(modelos.items())
        ]

    def _agregado(self, payload: dict) -> UsageRecord:
        uso = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        modelos = payload.get("modelUsage")
        nombres = sorted(modelos) if isinstance(modelos, dict) else []
        # Con un solo modelo el precio por token se puede atribuir; con varios no.
        tarifa = model_prices.get_price(self.vendor, nombres[0]) if len(nombres) == 1 else None
        return {
            "model": ",".join(nombres) or None,
            "input_tokens": uso.get("input_tokens"),
            "output_tokens": uso.get("output_tokens"),
            "cache_read_tokens": uso.get("cache_read_input_tokens"),
            "cache_creation_tokens": uso.get("cache_creation_input_tokens"),
            "web_search_requests": None,
            **_precios_aplicados(tarifa),
            # Versiones antiguas de la CLI usaban `cost_usd`.
            "cost_usd": payload.get("total_cost_usd", payload.get("cost_usd")),
            "session_id": payload.get("session_id"),
        }
