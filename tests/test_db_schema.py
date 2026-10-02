import sqlite3

import db_schema

CREATE = """
CREATE TABLE IF NOT EXISTS t (
    id    INTEGER,
    a     TEXT NOT NULL,
    b     REAL,
    PRIMARY KEY (id)
)
"""


def _tablas(conn):
    return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_crea_la_tabla_si_no_existe():
    conn = sqlite3.connect(":memory:")
    assert db_schema.ensure_table(conn, "t", CREATE) is None
    assert _tablas(conn) == ["t"]


def test_tabla_con_el_esquema_correcto_no_se_toca():
    conn = sqlite3.connect(":memory:")
    db_schema.ensure_table(conn, "t", CREATE)
    conn.execute("INSERT INTO t (id, a) VALUES (1, 'x')")
    assert db_schema.ensure_table(conn, "t", CREATE) is None
    assert conn.execute("SELECT a FROM t").fetchone() == ("x",)


def test_columna_de_mas_opcional_no_se_toca():
    # Una columna que el codigo no rellena pero que admite NULL no rompe los INSERT.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, a TEXT, b REAL, extra TEXT)")
    assert db_schema.ensure_table(conn, "t", CREATE) is None


def test_columna_de_mas_con_valor_por_defecto_no_se_toca():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, a TEXT, b REAL, extra TEXT NOT NULL DEFAULT 'x')")
    assert db_schema.ensure_table(conn, "t", CREATE) is None


def test_columna_de_mas_obligatoria_se_aparta(caplog):
    # El caso del antiguo usage_log.source: todas las columnas nuevas estan, pero
    # una obligatoria que el codigo ya no rellena haria fallar cada INSERT.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, a TEXT, b REAL, source TEXT NOT NULL)")
    conn.execute("INSERT INTO t VALUES (1, 'x', 1.0, 'bot')")

    apartada = db_schema.ensure_table(conn, "t", CREATE)

    assert apartada is not None
    conn.execute("INSERT INTO t (id, a) VALUES (2, 'y')")  # ya no falla
    assert conn.execute(f"SELECT source FROM {apartada}").fetchone() == ("bot",)
    assert "source" in caplog.text


def test_las_mayusculas_no_cuentan_como_columna_distinta():
    # SQLite no distingue mayusculas en nombres de columna.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (ID INTEGER, A TEXT, B REAL)")
    assert db_schema.ensure_table(conn, "t", CREATE) is None


def test_tabla_antigua_se_renombra_con_sus_datos_y_se_crea_la_nueva(caplog):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, viejo TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'dato')")

    apartada = db_schema.ensure_table(conn, "t", CREATE)

    assert apartada.startswith("t_old_")
    assert conn.execute(f"SELECT viejo FROM {apartada}").fetchone() == ("dato",)
    assert {r[1] for r in conn.execute("PRAGMA table_info(t)")} == {"id", "a", "b"}
    assert "faltaban ['a', 'b']" in caplog.text


def test_las_lineas_de_restriccion_no_cuentan_como_columnas():
    assert db_schema._columnas_declaradas(CREATE) == {"id", "a", "b"}


def test_parentesis_internos_comentarios_y_comillas():
    sql = """
    CREATE TABLE IF NOT EXISTS t (
        id      INTEGER,
        precio  DECIMAL(10,2),  -- con coma dentro del parentesis
        estado  TEXT CHECK (estado IN ('a', 'b')),
        "Raro"  TEXT,
        CONSTRAINT x UNIQUE (id, precio)
    )
    """
    assert db_schema._columnas_declaradas(sql) == {"id", "precio", "estado", "raro"}


def test_conexiones_concurrentes_apartan_la_tabla_una_sola_vez(tmp_path):
    import threading

    ruta = tmp_path / "c.db"
    with sqlite3.connect(ruta) as conn:
        conn.execute("CREATE TABLE t (id INTEGER, viejo TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'dato')")

    errores, barrera = [], threading.Barrier(6)

    def trabajador():
        conn = sqlite3.connect(ruta, timeout=30)
        try:
            barrera.wait()
            db_schema.ensure_table(conn, "t", CREATE)
        except Exception as exc:
            errores.append(exc)
        finally:
            conn.close()

    hilos = [threading.Thread(target=trabajador) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    with sqlite3.connect(ruta) as conn:
        apartadas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't_old_%'")]
        assert errores == []
        assert len(apartadas) == 1
        assert conn.execute(f"SELECT viejo FROM {apartadas[0]}").fetchone() == ("dato",)
        assert {r[1] for r in conn.execute("PRAGMA table_info(t)")} == {"id", "a", "b"}


def test_con_transaccion_ajena_abierta_no_hace_commit_de_ella():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE otra (x INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO otra VALUES (1)")  # transaccion de quien llama, abierta
    assert conn.in_transaction

    db_schema.ensure_table(conn, "t", CREATE)

    assert conn.in_transaction  # sigue abierta: no la ha confirmado
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM otra").fetchone() == (0,)


def test_si_falla_el_create_deshace_y_no_deja_la_transaccion_abierta():
    import pytest

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, viejo TEXT)")
    conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        db_schema.ensure_table(conn, "t", "CREATE TABLE t (id INTEGER, a TEXT, b REAL) SINTAXIS ROTA")

    assert not conn.in_transaction
    # El rename se deshizo: la tabla original sigue intacta y no hay ninguna apartada.
    assert {r[1] for r in conn.execute("PRAGMA table_info(t)")} == {"id", "viejo"}
    assert _tablas(conn) == ["t"]


def test_create_en_una_sola_linea():
    # Un analizador linea a linea solo veia 'id' aqui y daba por buena la tabla vieja.
    assert db_schema._columnas_declaradas("CREATE TABLE t (id INTEGER, a TEXT, b REAL)") == {"id", "a", "b"}


def test_comas_dentro_de_comentarios_y_textos_no_parten_columnas():
    sql = """CREATE TABLE t (
        id INTEGER, -- comentario, con coma
        x TEXT DEFAULT '--no, es comentario', /* bloque, con coma */ y TEXT
    )"""
    assert db_schema._columnas_declaradas(sql) == {"id", "x", "y"}


def test_el_analizador_coincide_con_sqlite_en_los_create_reales_del_repo(monkeypatch):
    """
    Lo que _columnas_declaradas entiende de cada CREATE del repo tiene que ser
    exactamente lo que SQLite crea. Si alguien escribe un CREATE que el analizador
    lee mal, este test lo dice antes de que una tabla vieja pase por buena.
    """
    import model_prices
    import usage_log

    capturados = {}
    real = db_schema.ensure_table

    def espia(conn, tabla, create_sql):
        capturados[tabla] = create_sql
        return real(conn, tabla, create_sql)

    monkeypatch.setattr(db_schema, "ensure_table", espia)
    usage_log._conn().close()
    model_prices._conn().close()

    assert set(capturados) == {"usage_log", "model_prices"}
    for tabla, sql in capturados.items():
        conn = sqlite3.connect(":memory:")
        conn.execute(sql)
        reales = {r[1].lower() for r in conn.execute(f"PRAGMA table_info({tabla})")}
        assert db_schema._columnas_declaradas(sql) == reales, tabla
