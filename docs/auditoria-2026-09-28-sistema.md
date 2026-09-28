# Auditoría del sistema — 28/09/2026

Revisión completa de foncu_assistant: los once módulos Python, la infraestructura
de despliegue, la suite de tests y cuatro pasadas transversales (seguridad,
concurrencia, manejo de errores, integridad de datos).

Complementa a `docs/decisions/2026-09-28-auditoria-configuracion-agentes.md`, que
cubría solo `CLAUDE.md` y `AGENT_CAPABILITIES.md`. Aquel alcance era demasiado
estrecho: los problemas más graves de este sistema no están en los `.md`.

Cada hallazgo indica el archivo y la línea. Lo que no pude verificar está al final,
separado y explícito.

---

## CRÍTICO

### C1 — El bot no comprueba quién le escribe

`handle_message` (main.py) no valida `chat_id` contra nada. `OWNER_CHAT_ID` solo se
usa para los avisos proactivos de DigitalOcean (líneas 629 y 747) y para decidir si
se registran los jobs diarios. No existe ninguna lista blanca, en ningún punto del
flujo.

Los bots de Telegram son públicos por diseño: quien conozca o adivine el @username
puede escribirle. Cualquiera que lo haga obtiene, sin autenticarse:

- Lectura y escritura completas sobre tu Todoist, incluido `eliminar_tarea`.
- El saldo y el gasto de tu cuenta de DigitalOcean.
- Y, encadenando cuatro llamadas, **ejecución de código en tu droplet**:
  `vincular_proyecto` → `crear_tarea` (con el contenido que quiera) →
  `vincular_carpeta_proyecto` (`/opt/foncu_assitant` está bajo `/opt`, que es la raíz
  permitida por defecto) → `ejecutar_tarea_dev`, que lanza Claude Code con
  `--permission-mode acceptEdits` sobre el repo.

El mapeo topic→proyecto es por `(chat_id, thread_id)`, así que un desconocido opera
en su propio espacio, pero las tools de Todoist y de DigitalOcean van contra **tus**
tokens, y la carpeta que vincule es una carpeta real de tu servidor.

Esto no lo introdujo ningún cambio reciente: lleva ahí desde que existe el bot. El
token filtrado esta mañana agrava el escenario, aunque ya esté rotado.

**Arreglo mínimo** — una guarda al principio de `handle_message` y de `handle_reset`:

```python
_ALLOWED_CHAT_IDS = {
    int(c) for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()
} or {int(OWNER_CHAT_ID)}

if chat_id not in _ALLOWED_CHAT_IDS:
    logger.warning("Mensaje rechazado de chat_id no autorizado: %s", chat_id)
    return  # silencio: no confirmar que el bot existe
```

Cuidado al desplegarlo: si `OWNER_CHAT_ID` no es correcto, te quedas fuera tú. Hoy
ya está validado al arrancar, así que el riesgo es bajo, pero compruébalo antes.

---

## ALTO

### A1 — Se puede perder el trabajo de una tarea si falla el commit automático

`execute_task_on_branch` (claude_code_executor.py) sigue este orden:

1. Se calcula `git_diff`, **truncado a 5000 caracteres**.
2. Si el árbol quedó sucio, `_fallback_commit()`.
3. Si ese commit falla, solo se anota `result["auto_commit_error"]`.
4. El `finally` llama a `_checkout_safe()`, que al ver el árbol sucio hace
   `git checkout -f` — y eso **descarta los cambios sin commitear**.

Resultado: cuando el commit de fallback falla, el trabajo de la tarea desaparece y
lo único que sobrevive son los primeros 5000 caracteres del diff, en un mensaje de
Telegram. El comentario del código asume que el diff los conserva, pero está
truncado.

**Arreglo:** si `_fallback_commit` falla, hacer `git stash push -u` antes del
checkout, o simplemente no volver a la rama original y avisar de que se queda ahí.

### A2 — `main.py` no tiene ni un solo test

El repo tiene 156 tests repartidos en once archivos, y `test_claude_code_executor.py`
es sólido (58 tests: clasificación, ramas, timeouts, fallback, migración del
dev_log). Pero **no existe `tests/test_main.py`**, y `main.py` son ~750 líneas que
concentran:

- El dispatch de las once tools (`execute_tool`).
- Los dos flujos de confirmación de Nivel 3.
- `_validate_project_directory()` — código de seguridad, escrito hoy, sin probar.
- `_validar_entorno()` y `_load_capabilities()`, de los que depende el arranque.
- La aritmética de la previsión de gasto de DigitalOcean.

Es el archivo con más lógica de negocio, todas las decisiones de seguridad, y cero
cobertura. Lo nuevo de hoy (`_run_reviewer`) tampoco tiene tests, mientras que su
gemelo `_get_plan` tiene seis.

### A3 — El reviewer revisa un diff truncado

`git_diff` se corta a 5000 caracteres (línea 52 de `execute_task_on_branch`). El
subagent `reviewer` recibe la instrucción de mirar `git diff original..rama`, así
que en principio lo calcula él por su cuenta con `Bash` — pero si en algún momento
se le pasa el diff ya truncado, o si el propio `git diff` desborda su contexto,
puede dar "sin hallazgos" sobre un cambio que no llegó a ver.

Una revisión que dice "todo bien" sobre lo que no ha mirado es peor que no tener
revisión, porque genera confianza. Conviene que el informe del reviewer indique
explícitamente cuántos archivos y líneas ha examinado.

### A4 — SQLite en modo por defecto con varios hilos escribiendo

Todos los módulos abren `sqlite3.connect(DB_PATH)` sin `PRAGMA journal_mode=WAL` y
sin `timeout` explícito. Contra esa misma base de datos escriben ahora:

- El hilo del bot (historial, memoria semántica, mapas).
- El hilo de uvicorn de `internal_api` (lecturas del dev_log).
- Los workers de `asyncio.to_thread` que introduje hoy.

En modo `delete`, un escritor bloquea la base entera y el resto espera hasta el
timeout por defecto (5 s) y luego lanza `database is locked`. Es el tipo de fallo
que no aparece en pruebas y sí en producción.

**Arreglo:** `PRAGMA journal_mode=WAL` y un `timeout` generoso en cada `_conn()`.
Es requisito previo para M1.

---

## MEDIO

### M1 — El arreglo del event loop no da concurrencia por sí solo

Verificado contra python-telegram-bot 22.7: `ApplicationBuilder` arranca con
`max_concurrent_updates=1`, o sea que procesa **un update cada vez**.

El `asyncio.to_thread` de hoy evita que se congele el event loop —los jobs
programados siguen disparando y el polling sigue vivo— pero un segundo mensaje
tuyo sigue esperando a que termine el primero. Para responder de verdad durante una
tarea de dev hace falta `.concurrent_updates(True)` en el builder, y eso **exige
antes A4**, o aparecerán bloqueos de SQLite.

### M2 — La memoria semántica crece sin límite y se busca linealmente

`conversation_history` se poda con `trim_and_summarize`, pero `semantic_memory` no
se poda nunca. Además `search_similar` hace un escaneo completo calculando
`vec_distance_cosine` fila a fila para ese `chat_id`: el coste crece lineal con el
histórico, en cada mensaje. `sqlite-vec` soporta tablas virtuales `vec0` con índice.

### M3 — La búsqueda semántica cruza topics

`search_similar` filtra por `chat_id` pero **no por `topic_id`**, aunque la tabla lo
guarda. Como cada topic es un proyecto, contenido de un topic personal puede acabar
inyectado en el system prompt de uno de trabajo. El docstring sugiere que es
deliberado ("recall across conversations"); si lo es, vale la pena dejarlo escrito,
y si no, es un `AND topic_id = ?` de nada.

### M4 — Las ramas se acumulan

Cada tarea crea `feat|fix|chore/<slug>-HHMMSS` y nadie las borra nunca. Con el
tiempo, decenas de ramas en el checkout del droplet. Sin riesgo, pero conviene una
política (borrar tras N días, o tras mergear).

### M5 — El README describe un proyecto que ya no existe

Dice *"Milestone 1: Telegram + Claude. Todavía no toca Todoist"*, y manda hacer
`cd telegram-claude-bot`. El sistema real tiene Todoist, ejecución de Claude Code,
memoria semántica, API interna y chequeos de DigitalOcean.

Importa más de lo que parece: es lo primero que lee cualquiera que llegue al repo,
Claude Code incluido.

### M6 — Tres versiones de Python distintas

`python:3.12-slim` en el Dockerfile, 3.14.3 en el venv local, y 3.13 en el CI (esto
último, error mío de esta tarde; ya corregido a 3.12). Que producción y desarrollo
no coincidan significa que un fallo específico de versión solo aparece en uno de los
dos sitios.

### M7 — `claude-code` sin pinnear

`npm install -g @anthropic-ai/claude-code` sin versión: cada `docker compose build`
puede traer una CLI distinta. Como el executor depende de flags concretos
(`--output-format json`, `--disallowedTools`, `--permission-mode`), un cambio
incompatible rompe el flujo en un despliegue que no tocaba nada de eso.

### M8 — `deploy.sh` no verifica nada después de desplegar

Hace `git pull`, `build`, `up -d` y muestra 30 líneas de log. No comprueba que el
contenedor siga vivo pasados unos segundos, ni ofrece rollback. Con los cambios de
hoy esto pesa más: `_validar_entorno()` y `_load_capabilities()` abortan el arranque
a propósito, así que un `.env` incompleto ahora deja el bot caído en vez de
arrancarlo a medias.

### M9 — Sin healthcheck

`restart: unless-stopped` reinicia si el proceso muere, pero si el bot se queda
colgado (bloqueo de SQLite, polling atascado) Docker lo ve como sano y no hace nada.

### M10 — El coste de Claude se tira

`execute_task` devuelve `cost_usd` (línea 186) y `_save_dev_log()` guarda tarea,
rama, resumen y plan — pero no el coste. Con tres invocaciones por tarea desde hoy,
no hay ninguna forma de saber lo que cuesta esto. Contrasta con el celo que se pone
en vigilar el gasto de DigitalOcean dos veces al día.

### M11 — Las funciones que faltaban para las fuentes de contenido ya existen

`content_sources.py` tiene `list_active_sources()` y `deactivate_source()`
implementadas y testeadas. Simplemente no están expuestas como tools en `main.py`.
La asimetría que anoté en el backlog se arregla con dos entradas en `TOOLS` y dos
ramas en `execute_tool`, no escribiendo nada nuevo.

---

## BAJO

- **B1** — `_git()` no captura `TimeoutExpired` ni `FileNotFoundError`. Se contiene
  aguas arriba en el `try/except` de `handle_message`, pero el mensaje de error que
  ve el usuario es opaco.
- **B2** — Cada `_conn()` ejecuta `CREATE TABLE IF NOT EXISTS` y hace `commit()`.
  Funciona, pero paga DDL en cada lectura.
- **B3** — `content_sources._conn()` llama a `_seed_defaults()`, que hace un
  `COUNT(*)` en cada operación.
- **B4** — `add_source` lanza `IntegrityError` si el nombre ya existe; llega al
  usuario como un volcado de excepción.
- **B5** — Email y nombre hardcodeados en el Dockerfile (`git config --global`).
- **B6** — Si falla el resumen en `trim_and_summarize`, los mensajes sobrantes no se
  borran y se reintenta el mismo resumen en cada mensaje posterior.
- **B7** — `sqlite3.Connection` usado como context manager gestiona la transacción,
  no el cierre. En CPython el contador de referencias lo cierra al salir de la
  función, así que no hay fuga real; es higiene, no un bug.

---

## Correcciones a lo que dije antes en la conversación

- **Dije que cambiar `AGENT_CAPABILITIES.md` requiere reiniciar el contenedor.** Es
  falso: el Dockerfile lo copia a `/app` y `_load_capabilities()` lee de ahí, no del
  volumen. Requiere `docker compose build`. Lo había escrito mal también en
  `docs/agent-architecture.md`; ya está corregido.
- **Dije que el `to_thread` haría que el bot siguiera respondiendo a otros mensajes.**
  Solo a medias, por M1.
- **Sospeché un bug de `.format()`** con llaves en el contenido de las tareas. Lo
  probé con las plantillas reales y es falso: `str.format` no reparsea los valores
  sustituidos. Descartado.

---

## Lo que no he podido verificar

1. **No he ejecutado los tests.** El venv del repo está construido para macOS con
   Python 3.14 y yo trabajo desde una VM Linux. Intenté montar uno aparte y me quedé
   sin disco (`sentence-transformers` arrastra PyTorch). **Todos los cambios de hoy
   están sin probar.**
2. **No veo el droplet.** No sé si el `.env` de producción tiene `ALLOWED_PROJECT_ROOTS`
   ni `REVIEWER_ENABLED`, ni el estado real de los contenedores. Con
   `_validar_entorno()` activo, un `.env` incompleto **impedirá el arranque**.
3. **No sé si el repo de GitHub es público o privado.** Cambia la gravedad de B5 y
   del `chat_id` que había en `.env.example`.
4. **No he auditado el texto del system prompt** línea a línea (ambigüedades,
   instrucciones contradictorias), solo su estructura y cómo se compone.
5. **La inyección de prompts no está evaluada a fondo.** El contenido de una tarea de
   Todoist llega literal a `claude -p` con `acceptEdits`. Hoy solo escribes tú esas
   tareas, así que el riesgo depende por completo de C1.
