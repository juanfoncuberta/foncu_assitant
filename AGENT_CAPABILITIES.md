# Agent Capabilities — foncu_assistant

Define qué acciones puede tomar el agente de forma autónoma, cuáles requieren aviso posterior,
y cuáles necesitan confirmación explícita previa. **Regla de oro:** la lista de tools disponibles
en el código para cada agente debe coincidir exactamente con lo que este documento autoriza.
Si una acción no aparece aquí como permitida, el agente no debe tener esa función disponible.

Estos niveles son propios de **foncu_assistant** como sistema, no de un canal concreto.
Hoy el único cliente es el bot de Telegram, pero si el día de mañana se conecta otro
cliente (web, WhatsApp, etc.) contra el mismo backend, hereda estos mismos niveles sin
tener que redefinirlos.

---

## Nivel 1 — Autónomo, sin aviso previo ni posterior

Acciones reversibles y de bajo impacto. El agente las ejecuta directamente sin notificar
al usuario antes ni después, salvo que el resultado en sí sea la respuesta esperada.

| Acción              | Descripción                                              |
|---------------------|----------------------------------------------------------|
| `listar_proyectos`  | Solo lectura, sin efectos secundarios                    |
| `vincular_proyecto` | Crea o vincula un proyecto Todoist al topic actual       |
| `crear_tarea`       | Escribe en Todoist; fácilmente reversible                |
| `listar_tareas`     | Solo lectura                                             |
| `actualizar_prioridad` | Cambio menor, reversible                              |
| `completar_tarea`   | Reversible (se puede descompletar)                       |
| `web_search`        | Solo lectura, sin riesgo                                 |

---

## Nivel 2 — Autónomo en ejecución, con aviso posterior obligatorio

El agente ejecuta sin pedir permiso previo, pero **siempre** informa del resultado al terminar.

| Acción               | Condición para ejecución autónoma                                      |
|----------------------|------------------------------------------------------------------------|
| `ejecutar_tarea_dev` | **Solo** cuando `directory_path` es un repositorio Git **Y** el árbol de trabajo está limpio (sin cambios sin commitear) antes de empezar |

**Aviso posterior obligatorio incluye:** resultado de la ejecución, diff generado (git diff),
coste en USD y session_id. La tarea de Todoist **nunca** se marca como completada
automáticamente — esa decisión la toma el usuario.

---

## Nivel 3 — Requiere confirmación explícita ANTES de ejecutar, sin excepción

| Acción                      | Motivo                                                              |
|-----------------------------|---------------------------------------------------------------------|
| `eliminar_tarea`            | Irreversible en Todoist (borrado real, no completar)                |
| `ejecutar_tarea_dev`        | Cuando `directory_path` **no** es un repo Git, o tiene cambios sin commitear — no hay red de seguridad para revertir |
| Publicación externa (futuro) | LinkedIn, X u otras plataformas públicas, cuando se implementen    |

Para `ejecutar_tarea_dev` en este caso, el agente debe:
1. Mostrar el contenido de la tarea y la carpeta donde se ejecutará.
2. Preguntar: "Voy a ejecutar la tarea X en la carpeta Y. ¿Confirmas?"
3. Solo proceder tras respuesta afirmativa explícita del usuario.

---

## Regla de oro

> La lista de tools disponibles en el código para cada agente debe coincidir exactamente
> con lo que este documento autoriza. Si una acción no está aquí como permitida, el agente
> no debe tener esa función disponible para llamar.

---

## Permisos de Claude Code dentro de `ejecutar_tarea_dev`

Los niveles de arriba gobiernan qué puede llamar foncu_assistant por su cuenta, sin importar qué cliente (Telegram u otro futuro) haya originado la petición.
Esta sección es distinta: gobierna qué puede hacer **Claude Code** una vez que
`claude_code_executor.py` lo invoca en modo headless para una tarea concreta.

Hoy `execute_task()` lanza `claude` con `--permission-mode acceptEdits` pero sin
ningún `--allowedTools` — es decir, no hay restricción real de herramientas todavía.
La tabla siguiente es el objetivo a implementar (ver tarea de allowlist explícito):

| Tipo de tarea                          | Tools permitidas                                                     |
|-----------------------------------------|------------------------------------------------------------------------|
| Consulta / lectura (sin cambios de código) | `Read`, `Glob`, `Grep` — sin `Edit`/`Write`, sin `Bash` de escritura |
| Cambio acotado con tests (caso normal)  | `Read`, `Glob`, `Grep`, `Edit`, `Write`, `Bash` limitado a `pytest`/lint, siempre en una rama nueva |
| Cambio estructural (varios módulos)    | Igual que el anterior + requiere que se haya declarado un plan (archivos a tocar y cambio en cada uno) antes de ejecutar |

**Archivos prohibidos para cualquier tipo de tarea, sin aprobación manual explícita:**
`.env`, `docker-compose.yml`, `Dockerfile`, `deploy.sh`, cualquier archivo bajo `.claude/`,
y cualquier migración de base de datos.
