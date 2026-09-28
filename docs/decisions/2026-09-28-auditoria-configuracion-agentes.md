# Auditoría de configuración de agentes

- **Fecha:** 2026-09-28
- **Estado:** aplicado (rama `chore/auditoria-agent-config`)
- **Disparador:** aplicar el árbol de decisión del artículo *"CLAUDE.md vs SKILL.md
  vs MCP: The Modern Agent Stack Explained"* (Satyam Sahu, sept 2026) a lo que ya
  había escrito en el repo.

## Contexto

`CLAUDE.md` y `AGENT_CAPABILITIES.md` habían crecido sin un criterio explícito de
qué va en cada uno. El objetivo era clasificar cada bloque; el hallazgo real fue
otro: varias reglas describían un comportamiento que el código no implementaba, y
otras estaban escritas para un agente que nunca las lee.

## Decisiones

### 1. Cada archivo pertenece a un agente, y solo a uno

`AGENT_CAPABILITIES.md` se carga en el system prompt del bot; `CLAUDE.md` lo carga
Claude Code. La sección *"Permisos de Claude Code dentro de ejecutar_tarea_dev"*
estaba en el primero, o sea que se enviaba en cada mensaje de Telegram al agente que
no decide eso, y no llegaba nunca al que sí.

**Decisión:** esa sección se mueve a `CLAUDE.md`. Ver `docs/agent-architecture.md`.

### 2. Lo irreversible se protege en código, no en prosa

`eliminar_tarea` pedía la confirmación con una frase dentro de la descripción de la
tool. `ejecutar_tarea_dev`, en cambio, ya devolvía `requires_confirmation` desde el
código. Dos acciones del mismo nivel con garantías muy distintas.

**Decisión:** `eliminar_tarea` sigue el mismo patrón — devuelve
`{requires_confirmation: true}` con el título y solo borra con `force_delete: true`.

### 3. Los archivos protegidos pasan de petición a candado

La lista (`.env`, `docker-compose.yml`, `Dockerfile`, `deploy.sh`, `.claude/`,
migraciones) solo existía en markdown.

**Decisión:** hook `PreToolUse` (`.claude/hooks/block_protected_files.py`) que
bloquea `Edit`/`Write` sobre esas rutas. La prosa en `CLAUDE.md` se queda como
explicación, no como mecanismo.

**Efecto secundario aceptado:** Claude Code ya no puede editar su propia
configuración bajo `.claude/`. Los cambios ahí son manuales.

### 4. El reviewer se conecta al flujo

`.claude/agents/reviewer.md` existía, estaba bien escrito, y no lo llamaba nadie.

**Decisión:** `_run_reviewer()` se ejecuta tras la tarea y su informe viaja en la
clave `review` del resultado. Es **detección, no prevención**: no bloquea nada, llega
después. Desactivable con `REVIEWER_ENABLED=false`, porque añade una tercera llamada
a `claude -p` por tarea.

### 5. La carpeta de un proyecto se valida al vincularla

`vincular_carpeta_proyecto` aceptaba cualquier ruta absoluta sin comprobar nada, y
esa ruta decide dónde se ejecutará Claude Code después.

**Decisión:** se valida que sea absoluta, que exista, y que caiga bajo
`ALLOWED_PROJECT_ROOTS` (`/opt` por defecto), resolviendo symlinks con `realpath`
para que no se pueda escapar con `..`. Si no es un repo Git se vincula igual, pero
con un aviso explícito: esa vía sigue cubierta por la confirmación de Nivel 3.

### 6. Una tool por entidad, no una por campo

`actualizar_prioridad` solo permitía cambiar la prioridad; no había forma de mover
una tarea de fecha ni de corregir su texto.

**Decisión:** sustituida por `actualizar_tarea` con campos opcionales. Se mantiene
`todoist_client.update_task_priority()` por compatibilidad con sus tests.

## Deriva encontrada entre documentación y código

Tres casos en los que el documento afirmaba algo falso. Son la razón de que exista
la checklist de `docs/agent-architecture.md`:

| Decía | Hacía |
|---|---|
| "no hay restricción real de herramientas todavía" | `_get_plan()` ya pasaba `--disallowedTools Edit,Write,Bash` |
| plan declarado solo para cambios estructurales | `_get_plan()` se llamaba para todas las tareas |
| "máximo 2 reintentos automáticos por tarea" | no existía ninguna lógica de reintento |

El tercero se reescribió como instrucción de comportamiento a Claude Code, que es
el único que puede cumplirla.

## Arreglos posteriores, en la misma rama

Al verificar la conexión del reviewer salieron cuatro problemas de producción que
no eran de configuración de agentes, pero que esta auditoría destapó:

### 7. El event loop estaba bloqueado en cada mensaje

`claude.messages.create()` y `execute_tool()` se llamaban de forma síncrona desde
`handle_message`, que es `async`. El bot dejaba de responder a todo el mundo durante
cada llamada a la API, y durante minutos enteros en un `ejecutar_tarea_dev`.

**Decisión:** ambas pasan por `asyncio.to_thread`. Además se envía un mensaje
provisional al arrancar una tarea de dev, porque el usuario espera minutos.

### 8. `_load_capabilities()` fallaba abierto

Si el archivo no se podía leer, devolvía `""` y el bot arrancaba **sin ninguna regla
de autonomía**, dejando solo una línea de log.

**Decisión:** falla cerrado — `RuntimeError` al arrancar, y además rechaza un archivo
que no contenga "Nivel 3" (truncado o vaciado). El coste aceptado es que un despliegue
con ese archivo roto deja el bot caído en vez de caído-pero-contestando.

### 9. No se validaba el entorno al arrancar

Origen del bug de `OWNER_CHAT_ID`: tenía el token del bot dentro, `int()` reventaba
dentro de un `try/except` que solo logueaba, y estuvo semanas sin que nadie lo notara.

**Decisión:** `_validar_entorno()` corre al importar el módulo. Variables críticas
ausentes o con forma incorrecta abortan el arranque con un mensaje que dice qué falla;
las opcionales solo avisan por log.

### 10. Sin CI

**Decisión:** `.github/workflows/tests.yml` ejecuta `pytest -m "not integration"` en
cada push. Los tests de integración cargan modelos de SentenceTransformer y se quedan
fuera por tiempo.

## Pendiente

1. `cost_usd` se devuelve pero no se persiste en `dev_log`. Ahora son tres llamadas
   a `claude -p` por tarea y no hay visibilidad del gasto acumulado.
2. `assistant.db` no tiene copia de seguridad (tarea creada en Todoist).
3. `vincular_carpeta_proyecto` sigue pidiendo `project_id` mientras el resto de tools
   trabaja con nombres de proyecto.
4. `anadir_fuente_contenido` es asimétrica: se puede añadir, no listar ni eliminar
   (tarea creada en Todoist).
