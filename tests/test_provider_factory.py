import subprocess
import sys
from pathlib import Path

import pytest

import provider_factory
from usage_provider import UsageProvider


@pytest.mark.parametrize("nombre", ["anthropic_api", "claude_code"])
def test_get_usage_provider_devuelve_un_usage_provider_con_ese_nombre(nombre):
    proveedor = provider_factory.get_usage_provider(nombre)
    assert isinstance(proveedor, UsageProvider)
    assert proveedor.name == nombre
    assert proveedor.vendor == "anthropic"


def test_get_usage_provider_desconocido_lanza_y_lista_opciones():
    with pytest.raises(ValueError, match="anthropic_api"):
        provider_factory.get_usage_provider("openai")


def test_get_task_provider_por_defecto_es_todoist(monkeypatch):
    monkeypatch.delenv("TASK_PROVIDER", raising=False)
    assert type(provider_factory.get_task_provider()).__name__ == "TodoistProvider"


def test_get_task_provider_desconocido_lanza(monkeypatch):
    monkeypatch.setenv("TASK_PROVIDER", "asana")
    with pytest.raises(ValueError, match="asana"):
        provider_factory.get_task_provider()



def test_registrar_gasto_no_carga_el_gestor_de_tareas():
    """
    usage_log no debe depender de Todoist: si Todoist (o httpx) no se pudiera
    importar, el gasto se tiene que poder registrar igual.
    """
    codigo = (
        "import sys, usage_log, provider_factory; "
        "provider_factory.get_usage_provider('anthropic_api'); "
        "print(sorted(m for m in ('todoist_client', 'todoist_provider', 'httpx') if m in sys.modules))"
    )
    raiz = Path(__file__).resolve().parent.parent
    salida = subprocess.run([sys.executable, "-c", codigo], cwd=raiz,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert salida == "[]"
