"""
Hace cumplir la "regla de oro" de AGENT_CAPABILITIES.md:

    la lista de tools disponibles en el codigo para cada agente debe coincidir
    exactamente con lo que este documento autoriza.

Hasta ahora eso era una frase en un markdown, o sea una peticion. Aqui pasa a ser
una comprobacion que corre en CI. Cuando alguien anada una tool a main.py y se
olvide de documentarla, este test falla y dice cual.

Se lee main.py con `ast` en vez de importarlo: importar main.py arranca la carga del
modelo de SentenceTransformer y exige un entorno completo, que es justo el motivo por
el que ese modulo nunca ha tenido tests.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"
CAPABILITIES = REPO / "AGENT_CAPABILITIES.md"


def _tools_declaradas_en_codigo() -> set[str]:
    """Nombres de las tools del literal TOOLS de main.py, sin importar el modulo."""
    arbol = ast.parse(MAIN.read_text(encoding="utf-8"))

    for nodo in arbol.body:
        if not isinstance(nodo, ast.Assign):
            continue
        destinos = [d.id for d in nodo.targets if isinstance(d, ast.Name)]
        if "TOOLS" not in destinos:
            continue

        nombres = set()
        for elemento in nodo.value.elts:
            for clave, valor in zip(elemento.keys, elemento.values):
                if isinstance(clave, ast.Constant) and clave.value == "name":
                    nombres.add(valor.value)
        return nombres

    raise AssertionError("No se encontro el literal TOOLS en main.py")


def _tools_documentadas() -> set[str]:
    """Primer nombre entre backticks de cada fila de tabla del documento."""
    documentadas = set()
    for linea in CAPABILITIES.read_text(encoding="utf-8").splitlines():
        if not linea.startswith("|") or linea.startswith("|--"):
            continue
        encontrado = re.search(r"`([a-z_]+)`", linea)
        if encontrado:
            documentadas.add(encontrado.group(1))
    return documentadas


class TestReglaDeOro:

    def test_toda_tool_del_codigo_esta_documentada(self):
        sin_documentar = _tools_declaradas_en_codigo() - _tools_documentadas()
        assert not sin_documentar, (
            f"Estas tools existen en main.py pero no aparecen en AGENT_CAPABILITIES.md: "
            f"{sorted(sin_documentar)}. Anadelas a la tabla del nivel que les corresponda."
        )

    def test_toda_tool_documentada_existe_en_el_codigo(self):
        fantasmas = _tools_documentadas() - _tools_declaradas_en_codigo()
        assert not fantasmas, (
            f"AGENT_CAPABILITIES.md autoriza tools que no existen en main.py: "
            f"{sorted(fantasmas)}. O se renombraron o se borraron sin actualizar el doc."
        )


class TestDocumentoUtilizable:
    """
    _load_capabilities() en main.py rechaza el arranque si este archivo no tiene
    'Nivel 3'. Estos tests avisan en CI en vez de en el despliegue.
    """

    def test_tiene_los_tres_niveles(self):
        contenido = CAPABILITIES.read_text(encoding="utf-8")
        for nivel in ("Nivel 1", "Nivel 2", "Nivel 3"):
            assert nivel in contenido, f"Falta la seccion '{nivel}'"

    def test_no_esta_vacio(self):
        assert len(CAPABILITIES.read_text(encoding="utf-8").strip()) > 500

    def test_las_acciones_irreversibles_siguen_en_nivel_3(self):
        contenido = CAPABILITIES.read_text(encoding="utf-8")
        nivel_3 = contenido[contenido.index("## Nivel 3"):]
        for accion in ("eliminar_tarea", "ejecutar_tarea_dev"):
            assert accion in nivel_3, (
                f"'{accion}' ya no esta en Nivel 3. Si el cambio es intencionado, "
                "actualiza este test explicando por que."
            )
