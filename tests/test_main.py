"""
Tests de main.py, empezando por lo que existe para IMPEDIR algo.

Importar main.py valida el entorno y lee AGENT_CAPABILITIES.md, asi que necesita
las variables criticas con la forma correcta (CI las define en el workflow; en
local las pone el .env). Ningun test habla con Telegram, Anthropic, Todoist ni
DigitalOcean: todo lo externo se sustituye con mocks.
"""

import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

import main

_ENV_VALIDO = {
    "TELEGRAM_BOT_TOKEN": "123456789:TEST-token-de-mentira-para-ci-0000000000",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "TODOIST_API_TOKEN": "test",
    "OWNER_CHAT_ID": "123456789",
    "DIGITALOCEAN_TOKEN": "do-test",
    "INTERNAL_API_KEY": "internal-test",
}


# ===========================================================================
# PRIORIDAD 1 — autorizacion de chats
# ===========================================================================


class TestCargarChatsAutorizados:
    def test_sin_allowed_chat_ids_autoriza_solo_al_owner(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
        monkeypatch.setattr(main, "OWNER_CHAT_ID", "111")
        assert main._cargar_chats_autorizados() == {111}

    def test_allowed_chat_ids_vacio_cae_a_owner(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_CHAT_IDS", "   ")
        monkeypatch.setattr(main, "OWNER_CHAT_ID", "111")
        assert main._cargar_chats_autorizados() == {111}

    def test_allowed_chat_ids_con_varios(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_CHAT_IDS", "111, -100222 ,333,")
        monkeypatch.setattr(main, "OWNER_CHAT_ID", "111")
        assert main._cargar_chats_autorizados() == {111, -100222, 333}

    def test_sin_owner_ni_allowed_no_autoriza_a_nadie(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
        monkeypatch.setattr(main, "OWNER_CHAT_ID", None)
        assert main._cargar_chats_autorizados() == set()

    def test_allowed_chat_ids_con_basura_falla_en_vez_de_ignorarla(self, monkeypatch):
        # Fallar al arrancar es lo correcto: ignorar el valor en silencio dejaria
        # una lista distinta de la que se cree configurada.
        monkeypatch.setenv("ALLOWED_CHAT_IDS", "111,no-es-un-id")
        with pytest.raises(ValueError):
            main._cargar_chats_autorizados()


class TestAutorizado:
    def test_chat_autorizado_pasa(self, monkeypatch):
        monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111, 222})
        assert main._autorizado(111) is True
        assert main._autorizado(222) is True

    def test_chat_no_autorizado_no_pasa(self, monkeypatch):
        monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
        assert main._autorizado(999) is False

    def test_lista_vacia_no_autoriza_a_nadie(self, monkeypatch):
        monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", set())
        assert main._autorizado(111) is False


class TestAvisoDeRechazo:
    def test_avisa_una_sola_vez_por_chat_dentro_del_intervalo(self, monkeypatch):
        monkeypatch.setattr(main, "_RECHAZOS_AVISADOS", {})
        reloj = iter([1000.0, 1000.0 + 10, 1000.0 + main._INTERVALO_AVISO_RECHAZO + 1])
        monkeypatch.setattr(main.time, "monotonic", lambda: next(reloj))

        assert main._debe_avisar_de_rechazo(999) is True
        assert main._debe_avisar_de_rechazo(999) is False
        assert main._debe_avisar_de_rechazo(999) is True

    def test_chats_distintos_se_avisan_por_separado(self, monkeypatch):
        monkeypatch.setattr(main, "_RECHAZOS_AVISADOS", {})
        monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
        assert main._debe_avisar_de_rechazo(1) is True
        assert main._debe_avisar_de_rechazo(2) is True


def _update_falso(chat_id: int, texto: str = "hola", thread_id: int | None = None):
    update = MagicMock()
    update.message.chat_id = chat_id
    update.message.text = texto
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    return update


def _context_falso():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


class TestHandlersRechazanChatsNoAutorizados:
    @pytest.fixture(autouse=True)
    def _entorno(self, monkeypatch):
        monkeypatch.setattr(main, "_CHATS_AUTORIZADOS", {111})
        monkeypatch.setattr(main, "OWNER_CHAT_ID", "111")
        monkeypatch.setattr(main, "_RECHAZOS_AVISADOS", {})
        # Si el rechazo fallara, cualquiera de estas llamadas lo delataria.
        self.get_summary = MagicMock(side_effect=AssertionError("no debe leer historial"))
        self.claude = MagicMock()
        self.reset_topic = MagicMock()
        monkeypatch.setattr(main, "get_summary", self.get_summary)
        monkeypatch.setattr(main, "claude", self.claude)
        monkeypatch.setattr(main, "reset_topic", self.reset_topic)

    def test_mensaje_de_desconocido_no_llega_a_claude_ni_se_contesta(self):
        update, context = _update_falso(999, "dame tus tareas"), _context_falso()

        asyncio.run(main.handle_message(update, context))

        self.claude.messages.create.assert_not_called()
        self.get_summary.assert_not_called()
        update.message.reply_text.assert_not_called()

    def test_mensaje_de_desconocido_avisa_al_owner(self):
        update, context = _update_falso(999, "dame tus tareas"), _context_falso()

        asyncio.run(main.handle_message(update, context))

        context.bot.send_message.assert_awaited_once()
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 111
        assert "999" in kwargs["text"]

    def test_reset_de_desconocido_no_borra_nada(self):
        update, context = _update_falso(999, "/reset"), _context_falso()

        asyncio.run(main.handle_reset(update, context))

        self.reset_topic.assert_not_called()
        update.message.reply_text.assert_not_called()

    def test_reset_del_owner_si_borra(self):
        update, context = _update_falso(111, "/reset", thread_id=7), _context_falso()

        asyncio.run(main.handle_reset(update, context))

        self.reset_topic.assert_called_once_with(111, 7)


# ===========================================================================
# PRIORIDAD 1 — validacion de carpetas de proyecto
# ===========================================================================


class TestValidateProjectDirectory:
    @pytest.fixture
    def raiz(self, tmp_path, monkeypatch):
        # realpath: en algunos sistemas el tmp es un symlink (macOS: /tmp -> /private/tmp).
        raiz = tmp_path.resolve() / "proyectos"
        raiz.mkdir()
        monkeypatch.setenv("ALLOWED_PROJECT_ROOTS", str(raiz))
        return raiz

    def test_ruta_valida_dentro_de_la_raiz(self, raiz):
        carpeta = raiz / "app"
        carpeta.mkdir()
        assert main._validate_project_directory(str(carpeta)) is None

    def test_la_propia_raiz_es_valida(self, raiz):
        assert main._validate_project_directory(str(raiz)) is None

    @pytest.mark.parametrize("ruta", ["", "proyectos/app", "./app", "~/app"])
    def test_ruta_relativa_o_vacia_rechazada(self, raiz, ruta):
        assert "absoluta" in main._validate_project_directory(ruta)

    def test_ruta_inexistente_rechazada(self, raiz):
        assert "no existe" in main._validate_project_directory(str(raiz / "no-existe"))

    def test_archivo_en_vez_de_carpeta_rechazado(self, raiz):
        archivo = raiz / "fichero.txt"
        archivo.write_text("x")
        assert "no existe o no es una carpeta" in main._validate_project_directory(str(archivo))

    def test_carpeta_fuera_de_las_raices_rechazada(self, raiz, tmp_path):
        fuera = tmp_path.resolve() / "fuera"
        fuera.mkdir()
        assert "fuera de las raices" in main._validate_project_directory(str(fuera))

    def test_carpeta_con_prefijo_parecido_a_la_raiz_rechazada(self, raiz):
        # /x/proyectos-malos empieza por "/x/proyectos" pero NO esta dentro.
        vecina = raiz.parent / (raiz.name + "-malos")
        vecina.mkdir()
        assert "fuera de las raices" in main._validate_project_directory(str(vecina))

    def test_dotdot_que_escapa_de_la_raiz_rechazado(self, raiz, tmp_path):
        (tmp_path.resolve() / "fuera").mkdir()
        ruta = f"{raiz}/../fuera"
        assert "fuera de las raices" in main._validate_project_directory(ruta)

    def test_dotdot_que_se_queda_dentro_es_valido(self, raiz):
        (raiz / "a").mkdir()
        (raiz / "b").mkdir()
        assert main._validate_project_directory(f"{raiz}/a/../b") is None

    def test_symlink_dentro_que_apunta_fuera_rechazado(self, raiz, tmp_path):
        fuera = tmp_path.resolve() / "fuera"
        fuera.mkdir()
        trampa = raiz / "trampa"
        trampa.symlink_to(fuera, target_is_directory=True)
        assert "fuera de las raices" in main._validate_project_directory(str(trampa))

    def test_symlink_dentro_que_apunta_dentro_es_valido(self, raiz):
        destino = raiz / "real"
        destino.mkdir()
        (raiz / "alias").symlink_to(destino, target_is_directory=True)
        assert main._validate_project_directory(str(raiz / "alias")) is None

    def test_varias_raices_separadas_por_comas(self, tmp_path, monkeypatch):
        r1, r2 = tmp_path.resolve() / "r1", tmp_path.resolve() / "r2"
        r1.mkdir()
        r2.mkdir()
        monkeypatch.setenv("ALLOWED_PROJECT_ROOTS", f"{r1}, {r2}")
        assert main._validate_project_directory(str(r2)) is None

    def test_por_defecto_la_raiz_es_opt(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_PROJECT_ROOTS", raising=False)
        assert main._allowed_project_roots() == ["/opt"]


class TestVincularCarpetaProyecto:
    """La validacion solo sirve si execute_tool la aplica antes de guardar."""

    @pytest.fixture
    def raiz(self, tmp_path, monkeypatch):
        raiz = tmp_path.resolve() / "proyectos"
        raiz.mkdir()
        monkeypatch.setenv("ALLOWED_PROJECT_ROOTS", str(raiz))
        self.set_dir = MagicMock()
        monkeypatch.setattr(main, "set_project_directory", self.set_dir)
        return raiz

    def test_carpeta_rechazada_no_se_guarda(self, raiz, tmp_path):
        fuera = tmp_path.resolve() / "fuera"
        fuera.mkdir()

        res = main.execute_tool(
            "vincular_carpeta_proyecto",
            {"project_id": "p1", "directory_path": str(fuera)},
            111, None,
        )

        assert "error" in res
        self.set_dir.assert_not_called()

    def test_carpeta_sin_git_se_guarda_con_aviso(self, raiz):
        carpeta = raiz / "app"
        carpeta.mkdir()

        res = main.execute_tool(
            "vincular_carpeta_proyecto",
            {"project_id": "p1", "directory_path": str(carpeta)},
            111, None,
        )

        assert res["status"] == "ok"
        assert "aviso" in res
        self.set_dir.assert_called_once_with("p1", str(carpeta))

    def test_carpeta_con_git_se_guarda_sin_aviso(self, raiz):
        carpeta = raiz / "app"
        (carpeta / ".git").mkdir(parents=True)

        res = main.execute_tool(
            "vincular_carpeta_proyecto",
            {"project_id": "p1", "directory_path": str(carpeta)},
            111, None,
        )

        assert res["status"] == "ok"
        assert "aviso" not in res


# ===========================================================================
# PRIORIDAD 1 — validacion del entorno al arrancar
# ===========================================================================


class TestValidarEntorno:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        for clave, valor in _ENV_VALIDO.items():
            monkeypatch.setenv(clave, valor)

    def test_entorno_correcto_no_lanza(self):
        main._validar_entorno()

    @pytest.mark.parametrize(
        "clave", ["TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "TODOIST_API_TOKEN", "OWNER_CHAT_ID"]
    )
    def test_falta_una_variable_critica(self, monkeypatch, clave):
        monkeypatch.delenv(clave)
        with pytest.raises(RuntimeError, match=clave):
            main._validar_entorno()

    def test_variable_critica_solo_con_espacios_cuenta_como_vacia(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            main._validar_entorno()

    def test_owner_chat_id_con_el_token_dentro(self, monkeypatch):
        # El incidente real: OWNER_CHAT_ID contenia el token del bot.
        monkeypatch.setenv("OWNER_CHAT_ID", _ENV_VALIDO["TELEGRAM_BOT_TOKEN"])
        with pytest.raises(RuntimeError, match="token") as exc:
            main._validar_entorno()
        # Solo se muestra el principio: el token completo no debe acabar en el log.
        assert _ENV_VALIDO["TELEGRAM_BOT_TOKEN"] not in str(exc.value)

    def test_owner_chat_id_no_numerico(self, monkeypatch):
        monkeypatch.setenv("OWNER_CHAT_ID", "juan")
        with pytest.raises(RuntimeError, match="OWNER_CHAT_ID"):
            main._validar_entorno()

    def test_owner_chat_id_negativo_es_valido(self, monkeypatch):
        # Los grupos de Telegram tienen chat_id negativo.
        monkeypatch.setenv("OWNER_CHAT_ID", "-100123456")
        main._validar_entorno()

    @pytest.mark.parametrize(
        "token", ["tu_token_de_botfather", "123456789", "abc:" + "x" * 40, "123:corto"]
    )
    def test_token_de_telegram_con_formato_invalido(self, monkeypatch, token):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            main._validar_entorno()

    def test_varios_errores_se_reportan_juntos(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        monkeypatch.setenv("OWNER_CHAT_ID", "juan")
        with pytest.raises(RuntimeError) as exc:
            main._validar_entorno()
        assert "ANTHROPIC_API_KEY" in str(exc.value)
        assert "OWNER_CHAT_ID" in str(exc.value)

    def test_opcionales_ausentes_solo_avisan(self, monkeypatch, caplog):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN")
        monkeypatch.delenv("INTERNAL_API_KEY")
        main._validar_entorno()
        assert "DIGITALOCEAN_TOKEN" in caplog.text
        assert "INTERNAL_API_KEY" in caplog.text


# ===========================================================================
# PRIORIDAD 1 — reglas de autonomia (fallo cerrado)
# ===========================================================================


class TestLoadCapabilities:
    @pytest.fixture
    def carpeta(self, tmp_path, monkeypatch):
        # _load_capabilities busca el archivo junto a main.py via __file__.
        monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
        return tmp_path

    def test_archivo_ausente_lanza(self, carpeta):
        with pytest.raises(RuntimeError, match="no arranca"):
            main._load_capabilities()

    def test_archivo_sin_nivel_3_lanza(self, carpeta):
        (carpeta / "AGENT_CAPABILITIES.md").write_text("## Nivel 1\n## Nivel 2\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Nivel 3"):
            main._load_capabilities()

    def test_archivo_vacio_lanza(self, carpeta):
        (carpeta / "AGENT_CAPABILITIES.md").write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError):
            main._load_capabilities()

    def test_archivo_valido_devuelve_el_contenido(self, carpeta):
        (carpeta / "AGENT_CAPABILITIES.md").write_text(
            "\n## Nivel 3 — confirmacion\nregla\n\n", encoding="utf-8"
        )
        assert main._load_capabilities() == "## Nivel 3 — confirmacion\nregla"

    def test_el_archivo_real_carga_y_entra_en_el_system_prompt(self):
        reglas = main._load_capabilities()
        assert "Nivel 3" in reglas
        assert reglas in main.SYSTEM_PROMPT


# ===========================================================================
# PRIORIDAD 2 — confirmaciones de Nivel 3
# ===========================================================================


class TestEliminarTarea:
    @pytest.fixture(autouse=True)
    def _mocks(self, monkeypatch):
        self.provider = MagicMock()
        self.todoist = MagicMock()
        self.todoist.get_task.return_value = {"content": "Comprar pan"}
        monkeypatch.setattr(main, "provider", self.provider)
        monkeypatch.setattr(main, "todoist_client", self.todoist)

    def test_sin_force_delete_pide_confirmacion_y_no_borra(self):
        res = main.execute_tool("eliminar_tarea", {"task_id": "t1"}, 111, None)

        assert res["requires_confirmation"] is True
        assert res["task_content"] == "Comprar pan"
        assert res["task_id"] == "t1"
        self.provider.delete_task.assert_not_called()

    def test_force_delete_false_explicito_tampoco_borra(self):
        main.execute_tool("eliminar_tarea", {"task_id": "t1", "force_delete": False}, 111, None)
        self.provider.delete_task.assert_not_called()

    def test_si_no_se_puede_leer_el_titulo_sigue_pidiendo_confirmacion(self):
        self.todoist.get_task.side_effect = RuntimeError("API caida")

        res = main.execute_tool("eliminar_tarea", {"task_id": "t1"}, 111, None)

        assert res["requires_confirmation"] is True
        self.provider.delete_task.assert_not_called()

    def test_con_force_delete_borra(self):
        res = main.execute_tool("eliminar_tarea", {"task_id": "t1", "force_delete": True}, 111, None)

        assert res == {"status": "eliminada"}
        self.provider.delete_task.assert_called_once_with("t1")


class TestEjecutarTareaDev:
    @pytest.fixture(autouse=True)
    def _mocks(self, monkeypatch):
        self.todoist = MagicMock()
        self.todoist.get_task.return_value = {"content": "anade un test", "project_id": "p1"}
        self.get_dir = MagicMock(return_value="/opt/app")
        self.git = MagicMock(return_value={"is_git": True, "is_clean": True})
        self.ejecutar = MagicMock(return_value={"status": "ok", "git_diff": ""})
        monkeypatch.setattr(main, "todoist_client", self.todoist)
        monkeypatch.setattr(main, "get_project_directory", self.get_dir)
        monkeypatch.setattr(main, "check_git_status", self.git)
        monkeypatch.setattr(main, "cc_execute_task_on_branch", self.ejecutar)

    def _llamar(self, **extra):
        return main.execute_tool("ejecutar_tarea_dev", {"task_id": "t1", **extra}, 111, None)

    def test_repo_limpio_ejecuta(self):
        res = self._llamar()
        assert res == {"status": "ok", "git_diff": ""}
        self.ejecutar.assert_called_once_with("anade un test", "/opt/app")

    def test_repo_sucio_pide_confirmacion_y_no_ejecuta(self):
        self.git.return_value = {"is_git": True, "is_clean": False}

        res = self._llamar()

        assert res["requires_confirmation"] is True
        assert "sin commitear" in res["reason"]
        self.ejecutar.assert_not_called()

    def test_carpeta_sin_git_pide_confirmacion_y_no_ejecuta(self):
        self.git.return_value = {"is_git": False, "is_clean": False}

        res = self._llamar()

        assert res["requires_confirmation"] is True
        assert "no es un repositorio Git" in res["reason"]
        self.ejecutar.assert_not_called()

    def test_force_execute_ejecuta_sin_mirar_git(self):
        self.git.return_value = {"is_git": False, "is_clean": False}

        self._llamar(force_execute=True)

        self.git.assert_not_called()
        self.ejecutar.assert_called_once()

    def test_proyecto_sin_carpeta_vinculada_no_ejecuta(self):
        self.get_dir.return_value = None

        res = self._llamar(force_execute=True)

        assert "vincular_carpeta_proyecto" in res["error"]
        self.ejecutar.assert_not_called()

    def test_tarea_sin_proyecto_no_ejecuta(self):
        self.todoist.get_task.return_value = {"content": "x"}

        res = self._llamar(force_execute=True)

        assert "project_id" in res["error"]
        self.ejecutar.assert_not_called()


# ===========================================================================
# PRIORIDAD 3 — dispatch y aritmetica de DigitalOcean
# ===========================================================================


def test_herramienta_desconocida_lanza():
    with pytest.raises(ValueError, match="desconocida"):
        main.execute_tool("borrar_todo", {}, 111, None)


class TestActualizarTarea:
    @pytest.fixture(autouse=True)
    def _mocks(self, monkeypatch):
        self.provider = MagicMock()
        monkeypatch.setattr(main, "provider", self.provider)

    def test_sin_campos_no_llama_al_proveedor(self):
        res = main.execute_tool("actualizar_tarea", {"task_id": "t1"}, 111, None)
        assert "error" in res
        self.provider.update_task.assert_not_called()

    def test_solo_pasa_los_campos_indicados(self):
        main.execute_tool("actualizar_tarea", {"task_id": "t1", "priority": 4}, 111, None)
        self.provider.update_task.assert_called_once_with(
            "t1", content=None, due_string=None, priority=4
        )


def _fecha_fija(monkeypatch, dia: date):
    class FechaFija(date):
        @classmethod
        def today(cls):
            return dia

    monkeypatch.setattr(main, "date", FechaFija)


class TestDigitalOceanSummary:
    def _con_balance(self, monkeypatch, balance):
        monkeypatch.setattr(main.digitalocean_client, "get_balance", lambda: balance)

    def test_proyeccion_lineal_a_fin_de_mes(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2026, 9, 10))  # septiembre: 30 dias
        self._con_balance(monkeypatch, {"month_to_date_usage": "12.00", "account_balance": "-5"})

        res = main._digitalocean_summary()

        assert res["projected_month_end_usage"] == 36.0
        assert res["account_balance"] == "-5"

    def test_primer_dia_del_mes(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2026, 10, 1))  # octubre: 31 dias
        self._con_balance(monkeypatch, {"month_to_date_usage": "1.00"})
        assert main._digitalocean_summary()["projected_month_end_usage"] == 31.0

    def test_ultimo_dia_de_febrero_bisiesto(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2028, 2, 29))
        self._con_balance(monkeypatch, {"month_to_date_usage": "29"})
        assert main._digitalocean_summary()["projected_month_end_usage"] == 29.0

    def test_mes_a_cero(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2026, 9, 15))
        self._con_balance(monkeypatch, {"month_to_date_usage": "0.00"})
        assert main._digitalocean_summary()["projected_month_end_usage"] == 0.0

    def test_sin_dato_de_uso_proyecta_cero(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2026, 9, 15))
        self._con_balance(monkeypatch, {})
        assert main._digitalocean_summary()["projected_month_end_usage"] == 0.0

    @pytest.mark.parametrize("valor", [None, "n/a"])
    def test_dato_de_uso_ilegible_proyecta_none(self, monkeypatch, valor):
        _fecha_fija(monkeypatch, date(2026, 9, 15))
        self._con_balance(monkeypatch, {"month_to_date_usage": valor})
        assert main._digitalocean_summary()["projected_month_end_usage"] is None

    def test_redondea_a_dos_decimales(self, monkeypatch):
        _fecha_fija(monkeypatch, date(2026, 9, 7))
        self._con_balance(monkeypatch, {"month_to_date_usage": "1.00"})
        assert main._digitalocean_summary()["projected_month_end_usage"] == 4.29
