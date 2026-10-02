"""
Proteccion de esquema para tablas que crea el propio codigo.

`CREATE TABLE IF NOT EXISTS` no hace nada si la tabla ya existe, aunque sea con un
esquema antiguo. Entonces cada INSERT falla, y si quien inserta se traga el error
(como usage_log, a proposito), el dato se pierde en silencio.

ensure_table() lo evita. Una tabla existente se considera INCOMPATIBLE si:
- le falta alguna columna que el codigo declara, o
- tiene una columna obligatoria (NOT NULL, sin valor por defecto) que el codigo ya
  no declara: el codigo no la rellenaria y cada INSERT fallaria.
En ese caso la RENOMBRA (no la borra: los datos quedan para revisarlos a mano), crea
la nueva y lo deja escrito en el log. Todo dentro de una transaccion exclusiva, para
que dos conexiones que lo ejecuten a la vez no aparten la tabla dos veces.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_NO_COLUMNAS = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}


def _info(conn: sqlite3.Connection, tabla: str) -> dict[str, tuple]:
    """{nombre_en_minusculas: (notnull, default, pk)} de las columnas actuales."""
    return {
        fila[1].lower(): (fila[3], fila[4], fila[5])
        for fila in conn.execute(f'PRAGMA table_info("{tabla}")')
    }


def _sin_comentarios(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(_quitar_comentario_de_linea(linea) for linea in sql.splitlines())


def _quitar_comentario_de_linea(linea: str) -> str:
    """Corta en '--' salvo que este dentro de comillas."""
    comilla = None
    for i, c in enumerate(linea):
        if comilla:
            if c == comilla:
                comilla = None
        elif c in "'\"`":
            comilla = c
        elif linea.startswith("--", i):
            return linea[:i]
    return linea


def _partes_de_primer_nivel(cuerpo: str) -> list[str]:
    """Separa por comas que no esten dentro de parentesis ni de comillas."""
    partes, actual, nivel, comilla = [], [], 0, None
    for c in cuerpo:
        if comilla:
            if c == comilla:
                comilla = None
        elif c in "'\"`[":
            comilla = "]" if c == "[" else c
        elif c == "(":
            nivel += 1
        elif c == ")":
            nivel -= 1
        elif c == "," and nivel == 0:
            partes.append("".join(actual))
            actual = []
            continue
        actual.append(c)
    partes.append("".join(actual))
    return [p.strip() for p in partes if p.strip()]


def _columnas_declaradas(create_sql: str) -> set[str]:
    """
    Nombres de columna (en minusculas) del CREATE TABLE, se escriba como se escriba:
    en una o varias lineas, con parentesis internos (DECIMAL(10,2), CHECK (...)),
    comentarios y nombres entre comillas.
    """
    sql = _sin_comentarios(create_sql)
    cuerpo = sql[sql.index("(") + 1: sql.rindex(")")]
    nombres = set()
    for parte in _partes_de_primer_nivel(cuerpo):
        m = re.match(r'\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))', parte)
        if not m:
            continue
        nombre = next(g for g in m.groups() if g)
        if m.group(4) and nombre.upper() in _NO_COLUMNAS:
            continue
        nombres.add(nombre.lower())
    return nombres


def _motivo_incompatible(actuales: dict[str, tuple], declaradas: set[str]) -> str | None:
    faltan = declaradas - set(actuales)
    if faltan:
        return f"faltaban {sorted(faltan)}"
    obligatorias_sobrantes = sorted(
        nombre for nombre, (notnull, default, pk) in actuales.items()
        if nombre not in declaradas and notnull and default is None and not pk
    )
    if obligatorias_sobrantes:
        return f"tenia columnas obligatorias que el codigo ya no rellena: {obligatorias_sobrantes}"
    return None


def ensure_table(conn: sqlite3.Connection, tabla: str, create_sql: str) -> str | None:
    """
    Crea `tabla` con `create_sql` si no existe; si existe con un esquema incompatible,
    la renombra a `<tabla>_old_<fecha>` y crea la nueva.
    Devuelve el nombre de la tabla apartada, o None si no hizo falta.
    """
    declaradas = _columnas_declaradas(create_sql)

    # Camino rapido, sin bloquear: lo normal es que la tabla ya este bien.
    actuales = _info(conn, tabla)
    if actuales and _motivo_incompatible(actuales, declaradas) is None:
        return None

    apartada = None
    # Si quien llama ya tiene una transaccion abierta, se trabaja dentro de ella (no se
    # puede abrir otra). Desde los _conn() del repo nunca la hay: se toma el bloqueo.
    propia = not conn.in_transaction
    if propia:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # Se vuelve a mirar con el bloqueo tomado: otra conexion puede haberlo
        # arreglado entre la comprobacion rapida y este punto.
        actuales = _info(conn, tabla)
        motivo = _motivo_incompatible(actuales, declaradas) if actuales else None
        if motivo:
            apartada = f"{tabla}_old_{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
            conn.execute(f'ALTER TABLE "{tabla}" RENAME TO "{apartada}"')
            # Los indices se van con la tabla renombrada pero conservan su nombre, y
            # el CREATE INDEX IF NOT EXISTS de la nueva se saltaria. Se quitan de la
            # apartada (los datos no se tocan).
            for _, indice, *_ in conn.execute(f'PRAGMA index_list("{apartada}")').fetchall():
                if not indice.startswith("sqlite_autoindex"):
                    conn.execute(f'DROP INDEX "{indice}"')
            logger.warning(
                "La tabla %s %s. Se ha renombrado a %s para no perder sus datos y se "
                "crea de nuevo. Revisala a mano.", tabla, motivo, apartada,
            )
        conn.execute(create_sql)
        if propia:
            conn.execute("COMMIT")
    except Exception:
        if propia:
            conn.execute("ROLLBACK")
        raise
    return apartada
