import config


def test_db_path_lee_el_entorno_en_cada_llamada(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/data/a.db")
    assert config.db_path() == "/data/a.db"
    monkeypatch.setenv("DB_PATH", "/data/b.db")
    assert config.db_path() == "/data/b.db"


def test_db_path_sin_variable_usa_el_defecto(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert config.db_path() == "assistant.db"


def test_db_path_vacia_o_con_espacios_usa_el_defecto(monkeypatch):
    # Un 'DB_PATH=' vacio en el .env haria que sqlite abriera una base temporal.
    monkeypatch.setenv("DB_PATH", "   ")
    assert config.db_path() == "assistant.db"


def test_timezone_por_defecto_es_madrid(monkeypatch):
    monkeypatch.delenv("TIMEZONE", raising=False)
    assert config.timezone().key == "Europe/Madrid"


def test_timezone_se_sobrescribe_con_la_variable(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    assert config.timezone().key == "America/New_York"
