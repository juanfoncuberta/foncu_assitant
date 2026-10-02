"""
Interfaz para leer el consumo de un proveedor de IA.

Cada proveedor (Anthropic por API, Claude Code por CLI, y el que venga despues)
devuelve el consumo en un formato distinto. Su UsageProvider lo traduce a un
registro normalizado, y usage_log lo guarda sin saber de quien viene.

Para anadir un proveedor: subclase de UsageProvider + alta en provider_factory.
Nada mas del sistema cambia.
"""

from abc import ABC, abstractmethod
from typing import Any, TypedDict


class UsageRecord(TypedDict):
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    web_search_requests: int | None
    # Precio por millon de tokens APLICADO en esta llamada (el vigente en ese
    # momento). None si no se conoce. Se guarda por fila porque los precios cambian.
    input_price_per_mtok: float | None
    output_price_per_mtok: float | None
    cache_read_price_per_mtok: float | None
    cache_write_price_per_mtok: float | None
    cost_usd: float | None  # None = no se pudo calcular; nunca se inventa
    session_id: str | None


class UsageProvider(ABC):

    #: Clave en provider_factory y valor de usage_log.client: la via por la que se
    #: llama al modelo, que es lo que decide el formato de la respuesta.
    name: str

    #: Quien cobra. Valor de usage_log.provider y clave de sus precios en model_prices.
    #: Varios clientes pueden compartir vendor (la API y Claude Code: 'anthropic').
    vendor: str

    @abstractmethod
    def parse(self, payload: Any) -> list[UsageRecord]:
        """
        Traduce la respuesta del proveedor a uno o varios UsageRecord: uno por modelo
        usado, para que el gasto de cada modelo quede con sus tokens y su precio.
        Puede lanzar si el payload no tiene el formato esperado: usage_log lo captura.
        """
