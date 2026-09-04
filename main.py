"""
Asistente personal — Milestone 2
Telegram <-> Claude <-> Todoist, con Topics de Telegram mapeados a proyectos.
"""

import json
import logging
import os
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import provider_factory
import todoist_client
from claude_code_executor import check_git_status, execute_task as cc_execute_task
from conversation_memory import add_message, get_history, get_summary, reset_topic, trim_and_summarize
from project_directory_map import get_directory as get_project_directory, set_directory as set_project_directory
from project_map import get_project_id, set_project_id

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

claude = Anthropic(api_key=ANTHROPIC_API_KEY)
provider = provider_factory.get_task_provider()

_BASE_SYSTEM_PROMPT = """Eres el asistente personal de Juan por Telegram.
Puedes responder cualquier pregunta de cualquier índole: conocimiento general, cálculos,
conversación, consejos, hora en otras ciudades, capitales, historia, ciencia, lo que sea.
Además, tienes herramientas para gestionar su agenda y tareas en Todoist, y para buscar
en la web.

USO DE web_search:

Hay preguntas que SIEMPRE requieren web_search, sin excepción. Saber calcular o razonar
una respuesta NO es lo mismo que saber el dato real. Usa web_search obligatoriamente en
estos casos, aunque creas que puedes deducir la respuesta:

- Hora actual en cualquier lugar del mundo. Aunque sepas el desfase horario de una ciudad,
  no sabes qué hora es "ahora mismo" sin buscar. Ejemplos: "¿Qué hora es en Tokio?",
  "¿A qué hora son las 3pm de NY aquí?"
- Fecha o día de la semana actual. Ejemplos: "¿Qué día es hoy?", "¿A cuántos estamos?"
- Cualquier pregunta que contenga "ahora", "actualmente", "hoy", "en este momento",
  "esta semana", "este mes" referida a un dato del mundo real.
- Noticias, eventos recientes, resultados deportivos, precios, cotizaciones, clima.

Para conocimiento general estable (capital de un país, fórmulas matemáticas, historia,
definiciones, biografías consolidadas), responde directamente sin necesidad de buscar.

Si tras buscar sigues sin encontrar una respuesta fiable, dilo honestamente en vez de
inventar o especular.

Cada conversación (topic de Telegram) puede estar vinculada a un proyecto de Todoist.

Si el topic actual NO tiene proyecto vinculado y Juan pide crear/listar/modificar una tarea:
1. Llama primero a listar_proyectos.
2. Si el contenido apunta claramente a uno de los proyectos existentes, propónselo para
   confirmar (ej. "Parece que esto es de Cuoco, ¿lo vinculo a ese proyecto?") en vez de
   preguntar en abierto.
3. Solo pregunta en abierto ("¿a qué proyecto pertenece?") si no hay ninguna pista razonable
   o hay ambigüedad entre varios proyectos.
Una vez confirmado, llama a vincular_proyecto y luego crea la tarea.

Si el topic ya tiene un proyecto vinculado y Juan no especifica el proyecto explícitamente,
la vinculación es un valor por defecto útil, no una asunción ciega. Antes de actuar:
- En la gran mayoría de los casos el contenido encaja con el proyecto vinculado: procede
  directamente sin preguntar.
- Si el contenido sugiere con claridad un proyecto DISTINTO al vinculado (una anomalía),
  o hay ambigüedad real entre varios proyectos posibles, pregúntale a Juan a qué proyecto
  pertenece antes de usar ninguna herramienta.
- Si el mensaje no tiene relación con ningún proyecto ni tarea (pregunta general, conversación
  normal), responde sin usar ninguna herramienta de Todoist.

Si Juan menciona explícitamente un proyecto distinto al del topic actual (ej. "créame
esto en el proyecto Cuoco", "lista las tareas de Trabajo"), usa el parámetro "proyecto"
de crear_tarea o listar_tareas con ese nombre — sin cambiar la vinculación del topic.

Cuando muestres tareas o proyectos, formatea la respuesta de forma clara y concisa.

IMPORTANTE — vincular_carpeta_proyecto asocia un proyecto de Todoist a una carpeta local
del servidor para poder ejecutar tareas de desarrollo en ella. Úsala cuando Juan quiera
configurar en qué directorio se ejecutan las tareas de un proyecto concreto."""


def _load_capabilities() -> str:
    capabilities_path = os.path.join(os.path.dirname(__file__), "AGENT_CAPABILITIES.md")
    try:
        with open(capabilities_path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.error("No se pudo leer AGENT_CAPABILITIES.md: %s", exc)
        return ""


_capabilities = _load_capabilities()
if _capabilities:
    SYSTEM_PROMPT = (
        _BASE_SYSTEM_PROMPT
        + "\n\n---\nEstas son tus reglas de capacidades y niveles de autonomía, síguelas estrictamente:\n\n"
        + _capabilities
    )
else:
    SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT

TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
    },
    {
        "name": "vincular_proyecto",
        "description": (
            "Busca un proyecto en Todoist por nombre. Si no existe, lo crea. "
            "Vincula ese proyecto al topic actual para que las tareas futuras "
            "se creen ahí automáticamente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Nombre del proyecto en Todoist",
                }
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "listar_proyectos",
        "description": "Lista todos los proyectos disponibles en Todoist.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "crear_tarea",
        "description": (
            "Crea una tarea en Todoist. Por defecto usa el proyecto vinculado al topic actual. "
            "Pasa 'proyecto' solo cuando Juan pida explícitamente otro proyecto distinto; "
            "en ese caso se busca o crea ese proyecto sin cambiar la vinculación del topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Texto de la tarea"},
                "date": {
                    "type": "string",
                    "description": "Fecha de vencimiento en lenguaje natural (ej: 'mañana', 'el lunes')",
                },
                "priority": {
                    "type": "integer",
                    "description": (
                        "Prioridad en escala API Todoist: "
                        "4=urgente (P1 app), 3=alta (P2), 2=media (P3), 1=sin prioridad (P4)"
                    ),
                    "enum": [1, 2, 3, 4],
                },
                "project": {
                    "type": "string",
                    "description": "Nombre del proyecto destino. Omitir para usar el del topic actual.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "listar_tareas",
        "description": (
            "Lista las tareas pendientes. Por defecto usa el proyecto vinculado al topic actual. "
            "Pasa 'proyecto' solo cuando Juan pida explícitamente las tareas de otro proyecto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Nombre del proyecto a consultar. Omitir para usar el del topic actual.",
                },
            },
        },
    },
    {
        "name": "actualizar_prioridad",
        "description": "Actualiza la prioridad de una tarea existente en Todoist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea"},
                "priority": {
                    "type": "integer",
                    "description": (
                        "Nueva prioridad: "
                        "4=urgente (P1 app), 3=alta (P2), 2=media (P3), 1=sin prioridad (P4)"
                    ),
                    "enum": [1, 2, 3, 4],
                },
            },
            "required": ["task_id", "priority"],
        },
    },
    {
        "name": "completar_tarea",
        "description": "Marca una tarea como completada en Todoist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea"}
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "vincular_carpeta_proyecto",
        "description": (
            "Asocia un proyecto de Todoist (por su project_id) a una ruta absoluta de carpeta "
            "en el servidor. Esta vinculación permite después ejecutar tareas de ese proyecto "
            "con ejecutar_tarea_dev. Si ya existía una carpeta vinculada, la reemplaza."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "ID del proyecto en Todoist",
                },
                "directory_path": {
                    "type": "string",
                    "description": "Ruta absoluta de la carpeta del proyecto en el servidor (ej: /home/user/projects/myapp)",
                },
            },
            "required": ["project_id", "directory_path"],
        },
    },
    {
        "name": "ejecutar_tarea_dev",
        "description": (
            "Ejecuta el contenido de una tarea de Todoist como prompt de Claude Code en la "
            "carpeta asociada al proyecto. Si la carpeta es un repo Git con árbol limpio, "
            "ejecuta directamente y devuelve el resultado con git_diff. Si no, devuelve "
            "{requires_confirmation: true} sin ejecutar; en ese caso pide confirmación al "
            "usuario y llama de nuevo con force_execute: true. Nunca completa la tarea "
            "automáticamente tras la ejecución."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID de la tarea de Todoist cuyo contenido se ejecutará",
                },
                "force_execute": {
                    "type": "boolean",
                    "description": (
                        "Si es true, ejecuta sin comprobar el estado del repositorio. "
                        "Usar solo tras confirmación explícita del usuario cuando la carpeta "
                        "no es un repo Git o tiene cambios sin commitear."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "eliminar_tarea",
        "description": (
            "Elimina permanentemente una tarea de Todoist (borrado real, no completar). "
            "Esta acción es irreversible. Antes de llamar a esta herramienta, confirma "
            "siempre con el usuario mostrando el título de la tarea, salvo que ya haya "
            "sido explícito y específico sobre qué tarea eliminar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea a eliminar"}
            },
            "required": ["task_id"],
        },
    },
]


def _resolve_project(name: str) -> dict:
    """Devuelve el proyecto con ese nombre; lo crea si no existe."""
    projects = provider.list_projects()
    project = next((p for p in projects if isinstance(p, dict) and p["name"].lower() == name.lower()), None)
    if project is None:
        project = provider.create_project(name)
    return project


def execute_tool(name: str, tool_input: dict[str, Any], chat_id: int, thread_id: int | None) -> Any:
    if name == "vincular_proyecto":
        project = _resolve_project(tool_input["project_name"])
        set_project_id(chat_id, thread_id, project["id"], project["name"])
        return {"status": "ok", "project_id": project["id"], "project_name": project["name"]}

    if name == "listar_proyectos":
        return provider.list_projects()

    if name == "crear_tarea":
        project = tool_input.get("project")
        project_id = _resolve_project(project)["id"] if project else get_project_id(chat_id, thread_id)
        return provider.create_task(
            content=tool_input["content"],
            due_string=tool_input.get("date"),
            priority=tool_input.get("priority", 1),
            project_id=project_id,
        )

    if name == "listar_tareas":
        project = tool_input.get("project")
        project_id = _resolve_project(project)["id"] if project else get_project_id(chat_id, thread_id)
        return provider.list_tasks(project_id=project_id)

    if name == "actualizar_prioridad":
        return provider.update_task_priority(tool_input["task_id"], tool_input["priority"])

    if name == "completar_tarea":
        provider.close_task(tool_input["task_id"])
        return {"status": "completada"}

    if name == "eliminar_tarea":
        provider.delete_task(tool_input["task_id"])
        return {"status": "eliminada"}

    if name == "vincular_carpeta_proyecto":
        set_project_directory(tool_input["project_id"], tool_input["directory_path"])
        return {
            "status": "ok",
            "project_id": tool_input["project_id"],
            "directory_path": tool_input["directory_path"],
        }

    if name == "ejecutar_tarea_dev":
        task = todoist_client.get_task(tool_input["task_id"])
        task_content = task.get("content", "")
        project_id = task.get("project_id")
        if not project_id:
            return {"error": "La tarea no tiene project_id asociado."}
        directory_path = get_project_directory(project_id)
        if not directory_path:
            return {
                "error": (
                    f"El proyecto {project_id} no tiene una carpeta vinculada. "
                    "Usa vincular_carpeta_proyecto primero."
                )
            }
        force = tool_input.get("force_execute", False)
        if not force:
            git = check_git_status(directory_path)
            if not git["is_git"] or not git["is_clean"]:
                reason = (
                    "La carpeta no es un repositorio Git."
                    if not git["is_git"]
                    else "El repositorio tiene cambios sin commitear."
                )
                return {
                    "requires_confirmation": True,
                    "reason": reason,
                    "task_content": task_content,
                    "directory_path": directory_path,
                }
        return cc_execute_task(task_content, directory_path)

    raise ValueError(f"Herramienta desconocida: {name}")


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    thread_id = update.message.message_thread_id
    reset_topic(chat_id, thread_id)
    logger.info("Historial borrado (chat=%s thread=%s)", chat_id, thread_id)
    await update.message.reply_text("Contexto borrado. Empezamos de cero.", message_thread_id=thread_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text
    chat_id = update.message.chat_id
    thread_id = update.message.message_thread_id

    logger.info("Mensaje recibido (chat=%s thread=%s): %s", chat_id, thread_id, user_text)

    summary = get_summary(chat_id, thread_id)
    system = SYSTEM_PROMPT
    if summary:
        system += f"\n\nResumen de la conversación anterior con el usuario:\n{summary}"

    messages: list[dict] = get_history(chat_id, thread_id) + [{"role": "user", "content": user_text}]

    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Tool use: %s %s", block.name, block.input)
            try:
                result = execute_tool(block.name, block.input, chat_id, thread_id)
            except Exception as exc:
                logger.exception("Error ejecutando herramienta %s", block.name)
                result = {"error": str(exc)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    reply_text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    await update.message.reply_text(reply_text, message_thread_id=thread_id)

    if reply_text:
        add_message(chat_id, thread_id, "user", user_text)
        add_message(chat_id, thread_id, "assistant", reply_text)
        trim_and_summarize(chat_id, thread_id, claude)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot arrancado. Esperando mensajes...")
    app.run_polling()


if __name__ == "__main__":
    main()
