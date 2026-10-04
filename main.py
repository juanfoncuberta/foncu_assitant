"""
Asistente personal — Milestone 2
Telegram <-> Claude <-> Todoist, con Topics de Telegram mapeados a proyectos.
"""

import asyncio
import calendar
import json
import logging
import os
import re
import threading
import time
from datetime import date, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import digitalocean_client
import internal_api
import model_prices
import provider_factory
import todoist_client
import usage_log
from claude_code_executor import check_git_status, execute_task_on_branch as cc_execute_task_on_branch
from content_sources import (
    add_source as add_content_source,
    deactivate_source as deactivate_content_source,
    list_active_sources as list_content_sources,
)
from conversation_memory import add_message, get_history, get_summary, reset_topic, trim_and_summarize
from project_directory_map import get_directory as get_project_directory, set_directory as set_project_directory
from project_map import get_project_id, get_project_label, set_project_id
from semantic_memory import add_semantic_memory, search_similar, warmup as warmup_embeddings

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def _validar_entorno() -> None:
    """
    Comprueba al arrancar que las variables criticas existen y tienen la forma esperada.

    Fallar aqui es barato: lo ves en el despliegue. Fallar dentro de un job nocturno
    envuelto en un try/except que solo loguea no se entera nadie — que es exactamente
    lo que paso con OWNER_CHAT_ID, que estuvo semanas con el token del bot dentro
    mientras int() reventaba en silencio cada noche.
    """
    errores: list[str] = []
    avisos: list[str] = []

    for clave in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "TODOIST_API_TOKEN"):
        if not os.environ.get(clave, "").strip():
            errores.append(f"{clave} no esta definida o esta vacia.")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        errores.append(
            "TELEGRAM_BOT_TOKEN no tiene forma de token de Telegram "
            "(se espera '<numero>:<cadena>')."
        )

    owner = os.environ.get("OWNER_CHAT_ID", "").strip()
    if not owner:
        errores.append(
            "OWNER_CHAT_ID no esta definida. Sin ella no hay avisos proactivos."
        )
    elif not re.fullmatch(r"-?\d+", owner):
        pista = (
            " Empieza por un numero seguido de ':', asi que te has dejado ahi el token "
            "del bot en vez del chat ID."
            if ":" in owner
            else ""
        )
        errores.append(
            f"OWNER_CHAT_ID debe ser un entero pelado y vale {owner[:14]!r}.{pista}"
        )

    for clave, para_que in (
        ("DIGITALOCEAN_TOKEN", "los chequeos de gasto"),
        ("INTERNAL_API_KEY", "la API interna en el puerto 8001"),
    ):
        if not os.environ.get(clave, "").strip():
            avisos.append(f"{clave} no esta definida: {para_que} no funcionaran.")

    for aviso in avisos:
        logger.warning("Configuracion: %s", aviso)

    if errores:
        raise RuntimeError(
            "Configuracion invalida en .env — el bot no arranca:\n  - "
            + "\n  - ".join(errores)
        )


_validar_entorno()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")

ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()


def _alerta_externa(asunto: str, detalle: str) -> None:
    """
    Canal de aviso INDEPENDIENTE de Telegram.

    Existe para un caso concreto: cuando lo que falla es el propio Telegram
    (OWNER_CHAT_ID mal configurado, bot bloqueado, chat inexistente), avisar por
    Telegram es imposible por definicion. Y un log no sirve de nada si nadie lo mira
    a diario.

    Apunta ALERT_WEBHOOK_URL a cualquier cosa que acepte un POST con JSON: el n8n que
    ya corre en el droplet (y desde ahi email, o lo que sea), Slack, Discord, etc.
    Asi el bot no necesita credenciales de correo ni saber como se entrega el aviso.

    Nunca lanza excepcion: un fallo avisando no puede tumbar lo que estaba avisando.
    """
    if not ALERT_WEBHOOK_URL:
        logger.error(
            "ALERTA SIN CANAL DE SALIDA (%s): %s -- configura ALERT_WEBHOOK_URL en el "
            ".env para recibir esto fuera de Telegram.",
            asunto, detalle,
        )
        return

    try:
        httpx.post(
            ALERT_WEBHOOK_URL,
            json={
                "origen": "foncu_assistant",
                "severidad": "critical",
                "asunto": asunto,
                "detalle": detalle,
            },
            timeout=10,
        )
        logger.info("Alerta externa enviada: %s", asunto)
    except Exception:
        logger.exception("No se pudo enviar la alerta externa: %s", asunto)


def _cargar_chats_autorizados() -> set[int]:
    """
    Chats que pueden hablar con el bot.

    Sin esto cualquiera que de con el @username del bot (son publicos por diseno)
    tiene acceso completo a Todoist, al gasto de DigitalOcean y, encadenando
    vincular_carpeta_proyecto con ejecutar_tarea_dev, a ejecutar codigo en el droplet.

    Por defecto solo el duenyo. ALLOWED_CHAT_IDS permite anadir mas (separados por
    comas) si algun dia el bot vive en un grupo.
    """
    raw = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
    if raw:
        return {int(c.strip()) for c in raw.split(",") if c.strip()}
    return {int(OWNER_CHAT_ID)} if OWNER_CHAT_ID else set()


_CHATS_AUTORIZADOS = _cargar_chats_autorizados()


def _autorizado(chat_id: int) -> bool:
    return chat_id in _CHATS_AUTORIZADOS


# Un desconocido insistiendo no debe convertirse en una lluvia de notificaciones
# (ni en una factura de Telegram): se avisa una vez por chat_id y hora.
_RECHAZOS_AVISADOS: dict[int, float] = {}
_INTERVALO_AVISO_RECHAZO = 3600  # segundos


def _debe_avisar_de_rechazo(chat_id: int) -> bool:
    ahora = time.monotonic()
    ultimo = _RECHAZOS_AVISADOS.get(chat_id)
    if ultimo is not None and ahora - ultimo < _INTERVALO_AVISO_RECHAZO:
        return False
    _RECHAZOS_AVISADOS[chat_id] = ahora
    return True


async def _avisar_acceso_rechazado(context, chat_id: int, texto: str) -> None:
    """Notifica al duenyo que alguien no autorizado ha escrito al bot."""
    if not OWNER_CHAT_ID or not _debe_avisar_de_rechazo(chat_id):
        return
    try:
        await context.bot.send_message(
            chat_id=int(OWNER_CHAT_ID),
            text=(
                "Acceso rechazado\n"
                f"chat_id: {chat_id}\n"
                f"Mensaje: {texto[:200]}\n\n"
                "Si eres tu, ALLOWED_CHAT_IDS o OWNER_CHAT_ID estan mal en el .env."
            ),
        )
    except Exception:
        logger.exception("No se pudo avisar del acceso rechazado de %s", chat_id)


_LOCAL_TZ = ZoneInfo("Europe/Madrid")

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
configurar en qué directorio se ejecutan las tareas de un proyecto concreto.

Usa anadir_fuente_contenido cuando Juan quiera registrar un nuevo feed o fuente de
contenido (blogs, RSS, newsletters) para el agente de LinkedIn/X.

Puedes consultar el gasto de infraestructura de Juan en DigitalOcean con
consultar_gasto_digitalocean (sin parámetros). Úsala cuando pregunte por el coste,
saldo o factura del mes en curso.

Puedes consultar lo que lleva gastado en modelos de IA (este bot y las tareas de
desarrollo) con consultar_gasto_ia. Úsala cuando pregunte cuánto cuesta el asistente
o el gasto en IA, sea del proveedor que sea. Para un proyecto concreto pásale
'project'. Si hay cualquier duda sobre a qué proyecto se refiere, o la tool responde
que no lo encuentra, no adivines: enséñale a Juan la lista 'proyectos_con_gasto' y
pregúntale cuál quiere.

Los precios de los modelos de IA están en la base de datos. consultar_precios_ia los
lista. actualizar_precio_ia los cambia o añade un modelo nuevo: es de Nivel 3, así que
la primera llamada solo devuelve el precio actual y el resultante; enséñaselos a Juan y
solo tras un sí explícito vuelve a llamarla con force_update: true."""


def _load_capabilities() -> str:
    """
    Carga las reglas de autonomia del agente.

    Falla CERRADO a proposito: si este archivo no se puede leer, el bot arrancaria
    sin ninguna regla de nivel 1/2/3 y seguiria contestando con normalidad, asi que
    nadie se enteraria de que las confirmaciones han desaparecido. Un componente de
    seguridad que se degrada en silencio es peor que no tenerlo.
    """
    capabilities_path = os.path.join(os.path.dirname(__file__), "AGENT_CAPABILITIES.md")
    try:
        with open(capabilities_path, encoding="utf-8") as f:
            contenido = f.read().strip()
    except OSError as exc:
        raise RuntimeError(
            f"No se pudo leer {capabilities_path}: {exc}. "
            "El bot no arranca sin sus reglas de autonomia."
        ) from exc

    if "Nivel 3" not in contenido:
        raise RuntimeError(
            f"{capabilities_path} no contiene la seccion 'Nivel 3'. "
            "Parece truncado o vacio; el bot no arranca sin sus reglas de autonomia."
        )

    return contenido


SYSTEM_PROMPT = (
    _BASE_SYSTEM_PROMPT
    + "\n\n---\nEstas son tus reglas de capacidades y niveles de autonomía, síguelas estrictamente:\n\n"
    + _load_capabilities()
)

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
        "name": "actualizar_tarea",
        "description": (
            "Actualiza una tarea existente en Todoist. Pasa solo los campos que cambian; "
            "los que omitas se quedan como están. Ojo: cambiar 'date' en una tarea "
            "recurrente sustituye su recurrencia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea"},
                "content": {"type": "string", "description": "Nuevo texto de la tarea"},
                "date": {
                    "type": "string",
                    "description": (
                        "Nueva fecha de vencimiento en lenguaje natural "
                        "(ej: 'mañana', 'el lunes')"
                    ),
                },
                "priority": {
                    "type": "integer",
                    "description": (
                        "Nueva prioridad: "
                        "4=urgente (P1 app), 3=alta (P2), 2=media (P3), 1=sin prioridad (P4)"
                    ),
                    "enum": [1, 2, 3, 4],
                },
            },
            "required": ["task_id"],
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
        "name": "anadir_fuente_contenido",
        "description": (
            "Registra una nueva fuente de contenido (blog, RSS, newsletter) para el agente "
            "de LinkedIn/X. Las fuentes activas son consultadas periódicamente para generar posts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Nombre descriptivo de la fuente (ej: 'One Useful Thing')",
                },
                "url": {
                    "type": "string",
                    "description": "URL del feed (ej: 'https://www.oneusefulthing.org/feed')",
                },
                "type": {
                    "type": "string",
                    "description": "Tipo de fuente: 'rss', 'newsletter', 'web', etc.",
                },
            },
            "required": ["name", "url", "type"],
        },
    },
    {
        "name": "listar_fuentes_contenido",
        "description": (
            "Lista las fuentes de contenido activas (RSS, newsletters, blogs) registradas "
            "para el agente de LinkedIn/X."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "desactivar_fuente_contenido",
        "description": (
            "Desactiva una fuente de contenido por su nombre exacto: deja de consultarse, "
            "pero no se borra de la base de datos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Nombre exacto de la fuente, tal como aparece en "
                        "listar_fuentes_contenido"
                    ),
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "eliminar_tarea",
        "description": (
            "Elimina permanentemente una tarea de Todoist (borrado real, no completar). "
            "Esta acción es irreversible. Llamada sin force_delete, devuelve "
            "{requires_confirmation: true} junto al título de la tarea, sin borrar nada; "
            "en ese caso muestra el título al usuario, pide confirmación explícita y solo "
            "entonces vuelve a llamar con force_delete: true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea a eliminar"},
                "force_delete": {
                    "type": "boolean",
                    "description": (
                        "Si es true, borra sin pedir confirmación. Usar solo tras una "
                        "respuesta afirmativa explícita del usuario al aviso previo."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "consultar_gasto_ia",
        "description": (
            "Consulta lo gastado en modelos de IA hoy y en el mes en curso: total, y "
            "desglosado por proyecto, por proveedor y por modelo (llamadas, tokens de "
            "entrada y salida, coste, precio por millón de tokens aplicado en el periodo "
            "y precio vigente hoy). Con 'project', solo ese proyecto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "Proyecto del que consultar el gasto. Omitir para el total. Si no "
                        "coincide con ninguno, la tool devuelve la lista de proyectos con gasto."
                    ),
                },
            },
        },
    },
    {
        "name": "consultar_precios_ia",
        "description": (
            "Lista los precios registrados de los modelos de IA (USD por millón de "
            "tokens, y por 1000 búsquedas web), con su fecha de consulta y fuente. "
            "Pasa 'provider' para filtrar por proveedor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Proveedor a filtrar (ej. 'anthropic'). Omitir para todos.",
                },
            },
        },
    },
    {
        "name": "actualizar_precio_ia",
        "description": (
            "Crea o cambia el precio de un modelo de IA en la base de datos. Afecta al "
            "cálculo de todo el gasto futuro. Los campos que no pases conservan su valor "
            "actual (en un modelo nuevo, valen 0). Sin force_update devuelve "
            "{requires_confirmation: true} con el precio actual y el resultante, sin "
            "guardar nada; tras confirmación explícita de Juan, llama de nuevo con "
            "force_update: true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "Proveedor (ej. 'anthropic')"},
                "model": {"type": "string", "description": "Id del modelo (ej. 'claude-sonnet-4-6')"},
                "input_per_mtok": {"type": "number", "description": "USD por millón de tokens de entrada"},
                "output_per_mtok": {"type": "number", "description": "USD por millón de tokens de salida"},
                "cache_write_per_mtok": {"type": "number", "description": "USD por millón de tokens escritos en caché (5 min)"},
                "cache_read_per_mtok": {"type": "number", "description": "USD por millón de tokens leídos de caché"},
                "web_search_per_1k": {"type": "number", "description": "USD por cada 1000 búsquedas web"},
                "checked_at": {"type": "string", "description": "Fecha en que se consultó el precio, YYYY-MM-DD"},
                "source_url": {"type": "string", "description": "URL de la página de precios consultada"},
                "force_update": {
                    "type": "boolean",
                    "description": "true solo tras una respuesta afirmativa explícita al aviso previo.",
                },
            },
            "required": ["provider", "model", "input_per_mtok", "output_per_mtok", "checked_at"],
        },
    },
    {
        "name": "consultar_gasto_digitalocean",
        "description": (
            "Consulta el saldo y el gasto del mes en curso en DigitalOcean. "
            "No requiere parámetros."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


_DEFAULT_PROJECT_ROOTS = "/opt"


def _allowed_project_roots() -> list[str]:
    """Raices bajo las que se permite vincular carpetas de proyecto."""
    raw = os.environ.get("ALLOWED_PROJECT_ROOTS", _DEFAULT_PROJECT_ROOTS)
    return [os.path.abspath(r.strip()) for r in raw.split(",") if r.strip()]


def _validate_project_directory(directory_path: str) -> str | None:
    """
    Comprueba que una carpeta puede vincularse a un proyecto.
    Devuelve el motivo del rechazo, o None si es valida.

    Esta carpeta determina donde se ejecutara Claude Code, asi que se valida dos
    veces: al vincularla (un fallo ahi es barato) y otra vez justo antes de cada
    ejecutar_tarea_dev (por si la carpeta, un symlink o las raices cambiaron).
    """
    if not directory_path or not os.path.isabs(directory_path):
        return "La ruta debe ser absoluta (empezar por '/')."

    # realpath resuelve symlinks y '..', para que no se pueda escapar de la raiz.
    real = os.path.realpath(directory_path)

    if not os.path.isdir(real):
        return f"'{directory_path}' no existe o no es una carpeta."

    roots = _allowed_project_roots()
    if not any(real == root or real.startswith(root + os.sep) for root in roots):
        return (
            f"'{directory_path}' esta fuera de las raices permitidas "
            f"({', '.join(roots)}). Si la carpeta es legitima, anadela a "
            "ALLOWED_PROJECT_ROOTS en el .env."
        )

    return None


def execute_tool(name: str, tool_input: dict[str, Any], chat_id: int, thread_id: int | None) -> Any:
    if name == "vincular_proyecto":
        project = provider.resolve_project(tool_input["project_name"])
        set_project_id(chat_id, thread_id, project["id"], project["name"])
        return {"status": "ok", "project_id": project["id"], "project_name": project["name"]}

    if name == "listar_proyectos":
        return provider.list_projects()

    if name == "crear_tarea":
        project = tool_input.get("project")
        project_id = provider.resolve_project(project)["id"] if project else get_project_id(chat_id, thread_id)
        return provider.create_task(
            content=tool_input["content"],
            due_string=tool_input.get("date"),
            priority=tool_input.get("priority", 1),
            project_id=project_id,
        )

    if name == "listar_tareas":
        project = tool_input.get("project")
        project_id = provider.resolve_project(project)["id"] if project else get_project_id(chat_id, thread_id)
        return provider.list_tasks(project_id=project_id)

    if name == "actualizar_tarea":
        campos = {
            clave: tool_input[clave]
            for clave in ("content", "date", "priority")
            if tool_input.get(clave) is not None
        }
        if not campos:
            return {"error": "No se indicó ningún campo que actualizar."}
        return provider.update_task(
            tool_input["task_id"],
            content=campos.get("content"),
            due_string=campos.get("date"),
            priority=campos.get("priority"),
        )

    if name == "completar_tarea":
        provider.close_task(tool_input["task_id"])
        return {"status": "completada"}

    if name == "eliminar_tarea":
        task_id = tool_input["task_id"]
        if not tool_input.get("force_delete", False):
            try:
                task_content = todoist_client.get_task(task_id).get("content", "")
            except Exception:
                logger.exception("No se pudo leer la tarea %s antes de borrarla", task_id)
                task_content = "(no se pudo leer el titulo de la tarea)"
            return {
                "requires_confirmation": True,
                "reason": "El borrado de una tarea es irreversible.",
                "task_id": task_id,
                "task_content": task_content,
            }
        provider.delete_task(task_id)
        return {"status": "eliminada"}

    if name == "anadir_fuente_contenido":
        return add_content_source(tool_input["name"], tool_input["url"], tool_input["type"])

    if name == "listar_fuentes_contenido":
        return list_content_sources()

    if name == "desactivar_fuente_contenido":
        return deactivate_content_source(tool_input["name"])

    if name == "vincular_carpeta_proyecto":
        escrita = tool_input["directory_path"]
        if not escrita or not os.path.isabs(escrita):
            return {"error": _validate_project_directory(escrita)}
        # Se guarda la ruta resuelta, no la escrita: si era un symlink y luego se
        # redirige, lo vinculado sigue siendo la carpeta que se valido. (El validador
        # vuelve a resolver por dentro; si algo cambiara justo entre esas dos llamadas,
        # es la misma ventana estructural que se acepta al ejecutar.)
        directory_path = os.path.realpath(escrita)
        rechazo = _validate_project_directory(directory_path)
        if rechazo:
            if directory_path != escrita:
                # El mensaje habla de la ruta resuelta; se anade la que escribio el
                # usuario para que entienda de donde sale.
                rechazo = f"La ruta indicada '{escrita}' lleva a '{directory_path}'. {rechazo}"
            return {"error": rechazo}

        set_project_directory(tool_input["project_id"], directory_path)
        respuesta = {
            "status": "ok",
            "project_id": tool_input["project_id"],
            "directory_path": directory_path,
        }
        if not os.path.isdir(os.path.join(directory_path, ".git")):
            respuesta["aviso"] = (
                "La carpeta no es un repositorio Git. Se ha vinculado igualmente, pero "
                "ejecutar_tarea_dev pedira confirmacion explicita cada vez, porque sin "
                "Git no hay forma de revertir los cambios."
            )
        return respuesta

    if name == "ejecutar_tarea_dev":
        task = todoist_client.get_task(tool_input["task_id"])
        task_content = task.get("content", "")
        project_id = task.get("project_id")
        if not project_id:
            return {"error": "La tarea no tiene project_id asociado."}
        vinculada = get_project_directory(project_id)
        if not vinculada:
            return {
                "error": (
                    f"El proyecto {project_id} no tiene una carpeta vinculada. "
                    "Usa vincular_carpeta_proyecto primero."
                )
            }
        # Se revalida AHORA, no solo al vincular: entre una cosa y otra pueden haber
        # cambiado la carpeta, un symlink o ALLOWED_PROJECT_ROOTS. Tambien protege
        # las rutas guardadas antes de que se guardaran resueltas. Se usa la ruta
        # resuelta aqui, no la guardada. Va antes de mirar Git y antes de
        # force_execute: confirmar no puede saltarse este limite. Queda una ventana
        # conocida y aceptada: el executor vuelve a usar esta ruta durante minutos.
        directory_path = os.path.realpath(vinculada) if os.path.isabs(vinculada) else vinculada
        rechazo = _validate_project_directory(directory_path)
        if rechazo:
            if directory_path != vinculada:
                rechazo = f"La carpeta vinculada '{vinculada}' lleva a '{directory_path}'. {rechazo}"
            return {"error": f"No se ejecuta: {rechazo}"}
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
        return cc_execute_task_on_branch(task_content, directory_path)

    if name == "consultar_gasto_digitalocean":
        return _digitalocean_summary()

    if name == "consultar_gasto_ia":
        pedido = tool_input.get("project")
        if not pedido:
            return usage_log.get_summary()
        proyecto = usage_log.resolve_project(pedido)
        if proyecto is None:
            return {
                "error": f"No hay gasto registrado para un proyecto llamado '{pedido}'.",
                "proyectos_con_gasto": usage_log.list_projects(),
            }
        return {"project": proyecto, **usage_log.get_summary(project=proyecto)}

    if name == "consultar_precios_ia":
        return model_prices.list_prices(tool_input.get("provider"))

    if name == "actualizar_precio_ia":
        pedidos = {
            k: tool_input[k]
            for k in ("provider", "model", "input_per_mtok", "output_per_mtok",
                      "cache_write_per_mtok", "cache_read_per_mtok", "web_search_per_1k",
                      "checked_at", "source_url")
            if tool_input.get(k) is not None
        }
        actual = model_prices.get_exact_price(pedidos.get("provider", ""), pedidos.get("model", ""))
        # Lo que no se pasa conserva su valor actual: actualizar entrada y salida no
        # puede poner a 0 la cache ni las busquedas sin que nadie lo vea.
        campos = {
            **{k: actual[k] for k in model_prices.PRICE_FIELDS + ("source_url",) if actual},
            **pedidos,
        }
        if not tool_input.get("force_update", False):
            return {
                "requires_confirmation": True,
                "reason": "Cambiar un precio altera el cálculo de todo el gasto a partir de ahora.",
                "precio_actual": actual,
                "precio_resultante": campos,
            }
        try:
            return {"status": "guardado", "precio": model_prices.upsert_price(**campos)}
        except (TypeError, ValueError) as exc:
            return {"error": str(exc)}

    raise ValueError(f"Herramienta desconocida: {name}")


def _digitalocean_summary() -> dict:
    """Fetches the current DO balance and adds a naive linear month-end spend forecast."""
    balance = digitalocean_client.get_balance()
    summary = dict(balance)
    try:
        month_usage = float(balance.get("month_to_date_usage", 0))
        today = date.today()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        day_of_month = today.day
        summary["projected_month_end_usage"] = round(
            (month_usage / day_of_month) * days_in_month, 2
        )
    except (TypeError, ValueError):
        summary["projected_month_end_usage"] = None
    return summary


def _format_do_summary_message(title: str, summary: dict) -> str:
    month_usage = summary.get("month_to_date_usage", "?")
    account_balance = summary.get("account_balance", "?")
    month_to_date_balance = summary.get("month_to_date_balance", "?")
    forecast = summary.get("projected_month_end_usage", "?")
    return (
        f"{title} — DigitalOcean\n"
        f"Gasto del mes en curso: ${month_usage}\n"
        f"Previsión de gasto a fin de mes: ${forecast}\n"
        f"Saldo mes a la fecha: ${month_to_date_balance}\n"
        f"Saldo de la cuenta: ${account_balance}"
    )


def _format_ia_line() -> str:
    """Linea de gasto en IA para el aviso diario. Nunca lanza."""
    try:
        resumen = usage_log.get_summary()
    except Exception:
        logger.exception("No se pudo leer usage_log para el aviso diario")
        return "IA: no se pudo leer el gasto (revisa los logs)."
    hoy, mes = resumen["hoy"], resumen["mes"]
    desglose = ", ".join(
        f"{proveedor} ${coste:.2f}" for proveedor, coste in sorted(mes["por_proveedor"].items())
    )
    linea = f"IA — hoy: ${hoy['total_usd']:.2f} · mes: ${mes['total_usd']:.2f}"
    if desglose:
        linea += f" ({desglose})"
    if mes["registros_sin_coste"]:
        linea += f"\nOjo: {mes['registros_sin_coste']} llamadas sin precio conocido no suman."
    return linea


async def _send_do_check(
    context: ContextTypes.DEFAULT_TYPE, title: str, incluir_ia: bool = False
) -> None:
    try:
        summary = _digitalocean_summary()
        text = _format_do_summary_message(title, summary)
        if incluir_ia:
            text += "\n\n" + _format_ia_line()
        await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
    except Exception as exc:
        logger.exception("Error en el chequeo de DigitalOcean (%s)", title)
        _alerta_externa(
            f"Fallo el chequeo de gasto ({title})",
            f"No se pudo completar ni enviar el aviso de DigitalOcean: {exc}",
        )


async def morning_do_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_do_check(context, "Aviso de la mañana")


async def evening_do_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_do_check(context, "Aviso de fin de jornada", incluir_ia=True)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    thread_id = update.message.message_thread_id

    if not _autorizado(chat_id):
        logger.warning("/reset rechazado: chat_id no autorizado (%s)", chat_id)
        return

    reset_topic(chat_id, thread_id)
    logger.info("Historial borrado (chat=%s thread=%s)", chat_id, thread_id)
    await update.message.reply_text("Contexto borrado. Empezamos de cero.", message_thread_id=thread_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text
    chat_id = update.message.chat_id
    thread_id = update.message.message_thread_id

    if not _autorizado(chat_id):
        # Al desconocido no se le contesta (confirmaria que el bot existe), pero al
        # duenyo si se le avisa: si el .env tuviera el chat_id mal, este seria el
        # unico sintoma visible de que te has dejado fuera a ti mismo.
        logger.warning(
            "Mensaje rechazado: chat_id no autorizado (%s). Texto: %.80s", chat_id, user_text
        )
        await _avisar_acceso_rechazado(context, chat_id, user_text or "")
        return

    logger.info("Mensaje recibido (chat=%s thread=%s): %s", chat_id, thread_id, user_text)

    summary = get_summary(chat_id, thread_id)
    system = SYSTEM_PROMPT
    if summary:
        system += f"\n\nResumen de la conversación anterior con el usuario:\n{summary}"

    history = get_history(chat_id, thread_id)
    history_contents = {m["content"] for m in history}

    similar = search_similar(chat_id, user_text)
    relevant = [m for m in similar if m["content"] not in history_contents]
    if relevant:
        lines = "\n".join(f"- [{m['role']}]: {m['content']}" for m in relevant)
        system += f"\n\nContexto relevante de conversaciones anteriores (similitud semántica):\n{lines}"

    messages: list[dict] = history + [{"role": "user", "content": user_text}]
    proyecto = await asyncio.to_thread(usage_log.project_of, get_project_label, chat_id, thread_id)

    while True:
        # to_thread: el cliente de Anthropic es sincrono. Llamarlo directo desde un
        # handler async congela el event loop entero — el bot deja de responder a
        # todo el mundo mientras espera.
        response = await asyncio.to_thread(
            claude.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # to_thread: escribe en SQLite y, con la base ocupada, no debe congelar el bot.
        await asyncio.to_thread(
            usage_log.record, "anthropic_api", proyecto, "chat", response,
            f"chat:{chat_id}/{thread_id}",
        )

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Tool use: %s %s", block.name, block.input)

            if block.name == "ejecutar_tarea_dev" and not block.input.get("force_execute"):
                await update.message.reply_text(
                    "Arrancando la tarea de desarrollo. Puede tardar varios minutos "
                    "(plan, ejecución y revisión); te aviso al terminar.",
                    message_thread_id=thread_id,
                )

            try:
                # Igual que arriba: execute_tool es sincrono y ejecutar_tarea_dev
                # puede tardar minutos lanzando claude -p tres veces.
                result = await asyncio.to_thread(
                    execute_tool, block.name, block.input, chat_id, thread_id
                )
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
        add_semantic_memory(chat_id, thread_id, "user", user_text)
        add_semantic_memory(chat_id, thread_id, "assistant", reply_text)
        # to_thread: resume con una llamada a Claude que tarda segundos. Hecha aqui
        # directamente bloquearia el event loop (y con el, los jobs programados).
        # Los mensajes siguientes siguen esperando su turno: el bot los atiende de uno
        # en uno.
        await asyncio.to_thread(trim_and_summarize, chat_id, thread_id, claude)


async def _saludo_de_arranque(app) -> None:
    """
    Comprueba contra la API de Telegram que OWNER_CHAT_ID es un chat real y alcanzable,
    y manda un mensaje de arranque.

    _validar_entorno() ya garantiza que es un entero, pero un entero puede ser
    perfectamente valido y aun asi no ser tu chat. Este es el unico momento en el que
    ese error se puede detectar antes de que te deje fuera del bot.
    """
    if not OWNER_CHAT_ID:
        return
    try:
        chat = await app.bot.get_chat(int(OWNER_CHAT_ID))
        logger.info("OWNER_CHAT_ID verificado: %s (%s)", OWNER_CHAT_ID, chat.type)
        await app.bot.send_message(
            chat_id=int(OWNER_CHAT_ID),
            text=f"Bot arrancado. Chats autorizados: {sorted(_CHATS_AUTORIZADOS)}",
        )
    except Exception as exc:
        logger.critical(
            "OWNER_CHAT_ID=%s no es un chat alcanzable. El bot sigue en pie, pero "
            "revisa el .env: es muy probable que no puedas hablarle.",
            OWNER_CHAT_ID,
        )
        # No se puede avisar por Telegram de que Telegram esta mal configurado.
        _alerta_externa(
            "OWNER_CHAT_ID no alcanzable",
            f"El bot arranco pero no puede escribir a OWNER_CHAT_ID={OWNER_CHAT_ID}. "
            f"Error: {exc}. Revisa el .env del droplet: probablemente no puedas "
            "hablarle al bot ni recibir los avisos de gasto.",
        )


def main() -> None:
    api_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": internal_api.app, "host": "0.0.0.0", "port": 8001, "log_level": "warning"},
        daemon=True,
    )
    api_thread.start()
    logger.info("Internal API listening on port 8001")

    # El modelo de embeddings se carga perezosamente para que importar este modulo
    # sea barato (y testeable). Se precalienta aqui, en segundo plano, para que el
    # primer mensaje no pague los ~20s de carga.
    threading.Thread(target=warmup_embeddings, daemon=True).start()

    # Siembra los precios de modelos que aun no esten en la base de datos. No pisa
    # los que ya existan (los actualizados con actualizar_precio_ia se respetan).
    model_prices.load_seed()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_saludo_de_arranque).build()
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Chequeo diario de gasto DO: 8:00 y 21:00, hora de Barcelona
    if OWNER_CHAT_ID:
        app.job_queue.run_daily(morning_do_check, time=dt_time(8, 0, tzinfo=_LOCAL_TZ))
        app.job_queue.run_daily(evening_do_check, time=dt_time(21, 0, tzinfo=_LOCAL_TZ))
    else:
        logger.error("OWNER_CHAT_ID no configurado, chequeos diarios de DigitalOcean desactivados")

    logger.info("Bot arrancado. Esperando mensajes...")
    app.run_polling()


if __name__ == "__main__":
    main()
