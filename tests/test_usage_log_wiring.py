"""
Comprueba que cada punto donde se gasta dinero en Claude apunta su consumo en
usage_log: los tres `claude -p` del executor (plan, exec, review), el chat del bot
y el resumen del historial. Si alguien quita una llamada a usage_log, el gasto de
ese punto desaparece del total sin que nada mas falle: por eso se testea aqui.
"""

import asyncio
import json
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import claude_code_executor as ce
import conversation_memory as cm
import config
import main
import usage_log

_CLI = {
    "result": "hecho",
    "session_id": "s1",
    "total_cost_usd": 0.25,
    "usage": {"input_tokens": 10, "output_tokens": 20,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    "modelUsage": {"claude-opus-4-8": {}},
}


def _filas():
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_log ORDER BY id")]
    except sqlite3.OperationalError:
        return []  # la tabla ni siquiera llego a crearse


def _cli_ok(*args, **kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(_CLI), stderr="")


def _respuesta_api(stop_reason="end_turn", content=None, model="claude-sonnet-4-6"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model=model,
        content=content if content is not None else [SimpleNamespace(type="text", text="hola")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, server_tool_use=None),
    )


# ---------------------------------------------------------------------------
# Claude Code (executor)
# ---------------------------------------------------------------------------


class TestExecutor:
    def test_execute_task_apunta_exec_con_su_origen(self, mocker):
        mocker.patch("subprocess.run", side_effect=_cli_ok)
        mocker.patch.object(ce, "get_git_diff", return_value="")

        ce.execute_task("tarea", "/home/x/repo", origin="feat/x-1")

        [fila] = _filas()
        assert (fila["provider"], fila["client"]) == ("anthropic", "claude_code")
        # El proyecto de una tarea de dev es el nombre de la carpeta del repo.
        assert (fila["project"], fila["step"]) == ("repo", "exec")
        assert fila["origin"] == "feat/x-1"
        assert fila["cost_usd"] == 0.25

    def test_execute_task_fallido_no_apunta_nada(self, mocker):
        mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"))
        ce.execute_task("tarea", "/repo")
        assert _filas() == []

    def test_plan_apunta_plan(self, mocker):
        mocker.patch("subprocess.run", side_effect=_cli_ok)
        ce._get_plan("tarea", "/repo", origin="feat/x-1")
        assert [(f["step"], f["origin"]) for f in _filas()] == [("plan", "feat/x-1")]

    def test_revision_apunta_review_con_la_rama(self, mocker, monkeypatch):
        monkeypatch.delenv("REVIEWER_ENABLED", raising=False)
        # git (lista de archivos del diff) va por _git; aqui no hay cambios.
        mocker.patch.object(
            ce, "_git", side_effect=lambda args, *a, **k: (0, "0" if args[0] == "rev-list" else "", "")
        )
        mocker.patch("subprocess.run", side_effect=_cli_ok)
        ce._run_reviewer("tarea", "plan", "feat/x-1", "main", "/repo")
        assert [(f["step"], f["origin"]) for f in _filas()] == [("review", "feat/x-1")]

    def test_revision_desactivada_no_apunta_nada(self, mocker, monkeypatch):
        monkeypatch.setenv("REVIEWER_ENABLED", "false")
        run = mocker.patch("subprocess.run", side_effect=_cli_ok)
        ce._run_reviewer("tarea", "plan", "feat/x-1", "main", "/repo")
        run.assert_not_called()
        assert _filas() == []

    def test_flujo_completo_etiqueta_los_tres_pasos_con_la_rama(self, mocker):
        def git_falso(args, cwd, timeout=30):
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[0] == "rev-list":
                return 0, "0", ""
            return 0, "", ""

        mocker.patch.object(ce, "_git", side_effect=git_falso)
        mocker.patch.object(ce, "_save_dev_log")
        mocker.patch.object(ce, "get_git_diff", return_value="")
        mocker.patch("subprocess.run", side_effect=_cli_ok)

        res = ce.execute_task_on_branch("add feature", "/repo")

        filas = _filas()
        assert [f["step"] for f in filas] == ["plan", "exec", "review"]
        assert {f["origin"] for f in filas} == {res["branch"]}
        assert {f["project"] for f in filas} == {"repo"}  # sin nombre: la carpeta


# ---------------------------------------------------------------------------
# Resumen del historial (Haiku)
# ---------------------------------------------------------------------------


def test_resumen_del_historial_apunta_summary(tmp_path, monkeypatch, precios_de_prueba):
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "cm.db"))
    monkeypatch.setattr(cm, "get_project_label", lambda chat, topic: "Cuoco")
    for i in range(cm.MAX_HISTORY + 1):
        cm.add_message(1, 7, "user", f"m{i}")
    respuesta = _respuesta_api(model="claude-haiku-4-5-20251001")
    cliente = MagicMock()
    cliente.messages.create.return_value = respuesta

    cm.trim_and_summarize(1, 7, cliente)

    [fila] = _filas()
    assert (fila["client"], fila["project"], fila["step"]) == ("anthropic_api", "Cuoco", "summary")
    assert fila["origin"] == "chat:1/7"
    assert fila["cost_usd"] == pytest.approx((100 * 0.5 + 50 * 4) / 1_000_000)


# ---------------------------------------------------------------------------
# Chat del bot (handle_message)
# ---------------------------------------------------------------------------


class TestHandleMessage:
    @pytest.fixture(autouse=True)
    def _entorno(self, monkeypatch, precios_de_prueba):
        monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
        for nombre, valor in {
            "get_summary": None, "get_history": [], "search_similar": [],
            "get_project_label": "Cuoco",
        }.items():
            monkeypatch.setattr(main, nombre, MagicMock(return_value=valor))
        for nombre in ("add_message", "add_semantic_memory", "trim_and_summarize"):
            monkeypatch.setattr(main, nombre, MagicMock())
        self.claude = MagicMock()
        monkeypatch.setattr(main, "claude", self.claude)

    def _update(self):
        update = MagicMock()
        update.message.chat_id = 111
        update.message.text = "hola"
        update.message.message_thread_id = 7
        update.message.reply_text = AsyncMock()
        return update

    def test_una_respuesta_apunta_una_llamada(self):
        self.claude.messages.create.return_value = _respuesta_api()

        asyncio.run(main.handle_message(self._update(), MagicMock()))

        [fila] = _filas()
        assert (fila["client"], fila["project"], fila["step"]) == ("anthropic_api", "Cuoco", "chat")
        assert fila["origin"] == "chat:111/7"
        assert fila["cost_usd"] == pytest.approx((100 * 2 + 50 * 10) / 1_000_000)

    def test_cada_vuelta_del_bucle_de_tools_cuenta(self, monkeypatch):
        tool_use = SimpleNamespace(type="tool_use", name="listar_proyectos", input={}, id="tu1")
        self.claude.messages.create.side_effect = [
            _respuesta_api(stop_reason="tool_use", content=[tool_use]),
            _respuesta_api(),
        ]
        monkeypatch.setattr(main, "execute_tool", MagicMock(return_value=[]))

        asyncio.run(main.handle_message(self._update(), MagicMock()))

        assert len(_filas()) == 2


# ---------------------------------------------------------------------------
# Tool y aviso diario
# ---------------------------------------------------------------------------


def test_tool_consultar_gasto_ia_devuelve_el_resumen(monkeypatch):
    monkeypatch.setattr(main.usage_log, "get_summary", lambda: {"hoy": {}, "mes": {}})
    assert main.execute_tool("consultar_gasto_ia", {}, 111, None) == {"hoy": {}, "mes": {}}


class TestAvisoDiario:
    @pytest.fixture(autouse=True)
    def _do(self, monkeypatch):
        monkeypatch.setattr(main, "OWNER_CHAT_ID", "111")
        monkeypatch.setattr(main, "_digitalocean_summary", lambda: {"month_to_date_usage": "3"})
        self.context = MagicMock()
        self.context.bot.send_message = AsyncMock()

    def _texto(self):
        return self.context.bot.send_message.await_args.kwargs["text"]

    def test_el_aviso_de_la_noche_incluye_el_gasto_en_ia_por_proveedor(self):
        usage_log.record("claude_code", "foncu_assitant", "exec", _CLI)
        asyncio.run(main.evening_do_check(self.context))
        assert "IA" in self._texto()
        assert "anthropic $0.25" in self._texto()

    def test_el_aviso_de_la_manana_no_incluye_ia(self):
        asyncio.run(main.morning_do_check(self.context))
        assert "IA" not in self._texto()

    def test_si_usage_log_falla_el_aviso_de_digitalocean_sale_igual(self, monkeypatch):
        monkeypatch.setattr(main.usage_log, "get_summary", MagicMock(side_effect=RuntimeError))
        asyncio.run(main.evening_do_check(self.context))
        assert "DigitalOcean" in self._texto()
        assert "no se pudo leer" in self._texto()

    def test_avisa_de_llamadas_sin_precio(self):
        usage_log.record("anthropic_api", None, "chat", _respuesta_api(model="claude-opus-9"))
        asyncio.run(main.evening_do_check(self.context))
        assert "sin precio" in self._texto()


# ---------------------------------------------------------------------------
# Tools de precios
# ---------------------------------------------------------------------------


class TestToolsDePrecios:
    NUEVO = {"provider": "anthropic", "model": "claude-sonnet-4-6", "input_per_mtok": 4.0,
             "output_per_mtok": 20.0, "checked_at": "2026-12-01",
             "source_url": "https://platform.claude.com/docs/en/about-claude/pricing"}

    def test_consultar_precios_lista_y_filtra(self, precios_de_prueba):
        import model_prices
        model_prices.upsert_price("otro", "x", 1.0, 1.0, "2026-01-01")

        todos = main.execute_tool("consultar_precios_ia", {}, 111, None)
        solo = main.execute_tool("consultar_precios_ia", {"provider": "otro"}, 111, None)

        assert len(todos) == 3
        assert [p["model"] for p in solo] == ["x"]

    def test_actualizar_sin_confirmar_no_guarda_y_ensena_actual_y_propuesto(self, precios_de_prueba):
        import model_prices

        res = main.execute_tool("actualizar_precio_ia", dict(self.NUEVO), 111, None)

        assert res["requires_confirmation"] is True
        assert res["precio_actual"]["input_per_mtok"] == 2.0
        assert res["precio_resultante"]["input_per_mtok"] == 4.0
        assert model_prices.get_price("anthropic", "claude-sonnet-4-6")["input_per_mtok"] == 2.0

    def test_actualizar_con_force_false_tampoco_guarda(self, precios_de_prueba):
        import model_prices
        main.execute_tool("actualizar_precio_ia", {**self.NUEVO, "force_update": False}, 111, None)
        assert model_prices.get_price("anthropic", "claude-sonnet-4-6")["input_per_mtok"] == 2.0

    def test_actualizar_con_confirmacion_guarda_y_cambia_el_calculo(self, precios_de_prueba):
        import model_prices
        from anthropic_usage_provider import estimate_cost

        res = main.execute_tool("actualizar_precio_ia", {**self.NUEVO, "force_update": True}, 111, None)

        assert res["status"] == "guardado"
        assert model_prices.get_price("anthropic", "claude-sonnet-4-6")["checked_at"] == "2026-12-01"
        assert estimate_cost("claude-sonnet-4-6", 1_000_000, 0) == 4.0

    def test_actualizar_modelo_nuevo_lo_anade(self):
        import model_prices
        nuevo = {**self.NUEVO, "model": "claude-opus-9", "force_update": True}

        main.execute_tool("actualizar_precio_ia", nuevo, 111, None)

        assert model_prices.get_price("anthropic", "claude-opus-9") is not None

    def test_actualizar_con_valor_invalido_devuelve_error_sin_guardar(self, precios_de_prueba):
        import model_prices
        malo = {**self.NUEVO, "input_per_mtok": -1, "force_update": True}

        res = main.execute_tool("actualizar_precio_ia", malo, 111, None)

        assert "input_per_mtok" in res["error"]
        assert model_prices.get_price("anthropic", "claude-sonnet-4-6")["input_per_mtok"] == 2.0


    def test_actualizar_conserva_los_campos_que_no_se_pasan(self, precios_de_prueba):
        # Solo entrada y salida: la cache y las busquedas no pueden quedarse a 0.
        import model_prices

        aviso = main.execute_tool("actualizar_precio_ia", dict(self.NUEVO), 111, None)
        main.execute_tool("actualizar_precio_ia", {**self.NUEVO, "force_update": True}, 111, None)

        assert aviso["precio_resultante"]["cache_read_per_mtok"] == 0.2
        assert aviso["precio_resultante"]["web_search_per_1k"] == 5.0
        guardado = model_prices.get_price("anthropic", "claude-sonnet-4-6")
        assert (guardado["cache_read_per_mtok"], guardado["cache_write_per_mtok"]) == (0.2, 2.5)
        assert guardado["web_search_per_1k"] == 5.0
        assert guardado["input_per_mtok"] == 4.0

    def test_actualizar_no_confunde_un_modelo_con_su_prefijo(self, precios_de_prueba):
        # Dar de alta 'claude-sonnet-4-6-20270101' no debe heredar ni pisar la fila base.
        import model_prices
        nuevo = {**self.NUEVO, "model": "claude-sonnet-4-6-20270101", "force_update": True}

        main.execute_tool("actualizar_precio_ia", nuevo, 111, None)

        assert model_prices.get_exact_price("anthropic", "claude-sonnet-4-6")["input_per_mtok"] == 2.0
        assert model_prices.get_exact_price("anthropic", "claude-sonnet-4-6-20270101")["cache_read_per_mtok"] == 0.0


def test_el_proyecto_del_flujo_de_dev_es_la_carpeta_aunque_acabe_en_barra(mocker):
    mocker.patch("subprocess.run", side_effect=_cli_ok)
    ce._get_plan("tarea", "/opt/foncu_assitant/", origin="feat/x-1")
    assert _filas()[0]["project"] == "foncu_assitant"


def test_el_chat_sin_proyecto_vinculado_se_apunta_sin_proyecto(monkeypatch):
    monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
    for nombre, valor in {"get_summary": None, "get_history": [], "search_similar": [],
                          "get_project_label": None}.items():
        monkeypatch.setattr(main, nombre, MagicMock(return_value=valor))
    for nombre in ("add_message", "add_semantic_memory", "trim_and_summarize"):
        monkeypatch.setattr(main, nombre, MagicMock())
    claude = MagicMock()
    claude.messages.create.return_value = _respuesta_api()
    monkeypatch.setattr(main, "claude", claude)
    update = MagicMock()
    update.message.chat_id, update.message.text, update.message.message_thread_id = 111, "hola", None
    update.message.reply_text = AsyncMock()

    asyncio.run(main.handle_message(update, MagicMock()))

    assert _filas()[0]["project"] == usage_log.SIN_PROYECTO


def test_si_falla_saber_el_proyecto_el_resumen_se_guarda_igual(tmp_path, monkeypatch, precios_de_prueba):
    # Caso real visto en local: topic_project con otro esquema -> la consulta lanza.
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "cm.db"))
    monkeypatch.setattr(cm, "get_project_label", MagicMock(side_effect=RuntimeError("boom")))
    for i in range(cm.MAX_HISTORY + 1):
        cm.add_message(1, 7, "user", f"m{i}")
    cliente = MagicMock()
    cliente.messages.create.return_value = _respuesta_api(model="claude-haiku-4-5")
    cliente.messages.create.return_value.content = [SimpleNamespace(text="resumen ok")]

    cm.trim_and_summarize(1, 7, cliente)

    assert cm.get_summary(1, 7) == "resumen ok"
    assert _filas()[0]["project"] == usage_log.SIN_PROYECTO


def test_si_falla_saber_el_proyecto_el_bot_contesta_igual(monkeypatch):
    monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
    for nombre, valor in {"get_summary": None, "get_history": [], "search_similar": []}.items():
        monkeypatch.setattr(main, nombre, MagicMock(return_value=valor))
    monkeypatch.setattr(main, "get_project_label", MagicMock(side_effect=RuntimeError("boom")))
    for nombre in ("add_message", "add_semantic_memory", "trim_and_summarize"):
        monkeypatch.setattr(main, nombre, MagicMock())
    claude = MagicMock()
    claude.messages.create.return_value = _respuesta_api()
    monkeypatch.setattr(main, "claude", claude)
    update = MagicMock()
    update.message.chat_id, update.message.text, update.message.message_thread_id = 111, "hola", 7
    update.message.reply_text = AsyncMock()

    asyncio.run(main.handle_message(update, MagicMock()))

    update.message.reply_text.assert_awaited_once()
    assert _filas()[0]["project"] == usage_log.SIN_PROYECTO


# ---------------------------------------------------------------------------
# Arranque y bloqueo del event loop
# ---------------------------------------------------------------------------


def test_main_siembra_los_precios_al_arrancar(monkeypatch):
    sembrar = MagicMock()
    monkeypatch.setattr(main.model_prices, "load_seed", sembrar)
    monkeypatch.setattr(main.threading, "Thread", MagicMock())
    app = MagicMock()
    builder = MagicMock()
    builder.token.return_value.post_init.return_value.build.return_value = app
    monkeypatch.setattr(main.Application, "builder", MagicMock(return_value=builder))

    main.main()

    sembrar.assert_called_once_with()
    app.run_polling.assert_called_once()


def test_el_registro_de_gasto_del_chat_va_fuera_del_event_loop(monkeypatch):
    """Escribir en SQLite con la base ocupada no puede congelar el bot entero."""
    monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
    for nombre, valor in {"get_summary": None, "get_history": [], "search_similar": [],
                          "get_project_label": "Cuoco"}.items():
        monkeypatch.setattr(main, nombre, MagicMock(return_value=valor))
    for nombre in ("add_message", "add_semantic_memory"):
        monkeypatch.setattr(main, nombre, MagicMock())

    def trim_and_summarize(*args):  # funcion con nombre real, para reconocerla
        pass

    monkeypatch.setattr(main, "trim_and_summarize", trim_and_summarize)
    claude = MagicMock()
    claude.messages.create.return_value = _respuesta_api()
    monkeypatch.setattr(main, "claude", claude)

    en_hilo = []
    to_thread_real = asyncio.to_thread

    async def espia(func, *args, **kwargs):
        en_hilo.append(getattr(func, "__name__", repr(func)))
        return await to_thread_real(func, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", espia)
    update = MagicMock()
    update.message.chat_id, update.message.text, update.message.message_thread_id = 111, "hola", 7
    update.message.reply_text = AsyncMock()

    asyncio.run(main.handle_message(update, MagicMock()))

    assert "record" in en_hilo              # usage_log.record
    assert "project_of" in en_hilo          # averiguar el proyecto
    assert "trim_and_summarize" in en_hilo  # el resumen del historial (llama a Claude)


# ---------------------------------------------------------------------------
# consultar_gasto_ia por proyecto
# ---------------------------------------------------------------------------


class TestGastoPorProyecto:
    @pytest.fixture(autouse=True)
    def _datos(self, precios_de_prueba):
        usage_log.record("anthropic_api", "foncu_assitant", "chat", _respuesta_api())
        usage_log.record("anthropic_api", "Cuoco", "chat", _respuesta_api())

    def test_sin_proyecto_da_el_total(self):
        res = main.execute_tool("consultar_gasto_ia", {}, 111, None)
        assert set(res["mes"]["por_proyecto"]) == {"foncu_assitant", "Cuoco"}

    def test_con_proyecto_solo_ese_y_sin_distinguir_mayusculas(self):
        res = main.execute_tool("consultar_gasto_ia", {"project": "cuoco"}, 111, None)
        assert res["project"] == "Cuoco"
        assert set(res["mes"]["por_proyecto"]) == {"Cuoco"}
        assert res["mes"]["llamadas"] == 1

    def test_proyecto_desconocido_no_adivina_y_lista_los_que_hay(self):
        res = main.execute_tool("consultar_gasto_ia", {"project": "foncu"}, 111, None)
        assert "foncu" in res["error"]
        assert res["proyectos_con_gasto"] == ["Cuoco", "foncu_assitant"]
        assert "mes" not in res
