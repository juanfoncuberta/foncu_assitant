# Arquitectura de agentes

Mapa de qué agentes existen en foncu_assistant, cómo se configura cada uno, y
dónde tiene que vivir cada norma. Si dudas de dónde poner algo nuevo, la respuesta
está aquí.

---

## Los dos agentes

En este proyecto conviven **dos** IAs distintas. No comparten configuración, ni
contexto, ni memoria. Confundirlas es la causa habitual de que una regla se escriba
en un sitio donde nadie la lee.

### Agente A — el bot de Telegram

- **Dónde vive:** `main.py`, proceso largo dentro del contenedor `bot`.
- **Qué es:** un bucle de Python que llama a la API de Anthropic con un system
  prompt y una lista de tools.
- **Cómo se configura:**
  - System prompt = `_BASE_SYSTEM_PROMPT` + el contenido de `AGENT_CAPABILITIES.md`,
    concatenados en `main.py` al importar el módulo (`_load_capabilities()`).
  - Herramientas = la lista `TOOLS` de `main.py`.
- **Consecuencia importante:** todo lo que esté en su system prompt se envía en
  **cada mensaje**. No hay carga bajo demanda. Cada párrafo de más se paga siempre.
- **No lee `CLAUDE.md`.** Ese archivo no le afecta en absoluto.
- Cambiar `AGENT_CAPABILITIES.md` requiere **reconstruir la imagen**
  (`docker compose build`), no solo reiniciar: el Dockerfile lo copia a `/app` y
  `_load_capabilities()` lee de ahí, no del volumen montado. `deploy.sh` ya
  reconstruye, pero un `docker compose restart` a secas no sirve.
- `CLAUDE.md` y `.claude/` **no** entran en la imagen (`.dockerignore`). Llegan por
  el bind mount `/opt/foncu_assitant`, que es donde Claude Code trabaja. Por eso
  funcionan hoy, y por eso dejarían de funcionar si algún día se ejecutara una tarea
  sobre una carpeta no montada.

### Agente B — Claude Code

- **Dónde vive:** proceso efímero, lanzado por `claude_code_executor.py` con
  `subprocess.run(["claude", "-p", ...])`.
- **Qué es:** el CLI de Claude Code en modo headless, trabajando dentro de
  `directory_path`.
- **Cómo se configura:** por convenciones que el propio CLI busca al arrancar —
  `CLAUDE.md` de la carpeta, `.claude/settings.json`, `.claude/agents/`,
  `.claude/skills/` — más los flags del comando (`--disallowedTools`,
  `--permission-mode`).
- **No carga `AGENT_CAPABILITIES.md`** en su contexto. Solo `CLAUDE.md` se auto-carga.

### Cómo se relacionan

El agente A **lanza** al agente B, como quien abre un programa. No es que uno "use"
al otro: son procesos separados.

```
Telegram → Agente A (main.py) → ejecutar_tarea_dev → Agente B (claude -p) → repo
```

Hoy cada tarea de dev son **tres** invocaciones del agente B:

1. `_get_plan()` — solo lectura (`--disallowedTools Edit,Write,Bash`). Declara qué
   piensa hacer, antes de tocar nada. Se guarda en `dev_log.plan`.
2. `execute_task()` — la ejecución real, en una rama nueva.
3. `_run_reviewer()` — solo lectura. Compara el diff real contra el plan declarado.

---

## Dónde va cada norma

De más fuerte a más débil. **Baja siempre todo lo que puedas**: una regla en prosa
es una petición, una regla en código es una garantía.

| Nivel | Dónde | Para qué | Se salta si… |
|---|---|---|---|
| 1 | **Código** (`main.py`, `*_client.py`) | Lo que no puede depender de que el modelo se acuerde: confirmaciones, validación de rutas, límites | nunca |
| 2 | **Hooks** (`.claude/hooks/`) | Reglas mecánicas y deterministas sobre acciones de Claude Code: archivos protegidos, tests | nunca, dentro de Claude Code |
| 3 | **`CLAUDE.md`** | Reglas siempre ciertas para Claude Code: convenciones, arquitectura, límites | el modelo se despista |
| 4 | **`.claude/skills/`** | Procedimientos de una tarea concreta y repetible | no encaja la descripción |
| 5 | **`AGENT_CAPABILITIES.md`** | System prompt del bot: niveles de autonomía | el modelo se despista |
| 6 | **`docs/`** | El porqué: arquitectura, decisiones, runbooks | no lo lee nadie automáticamente |

### Árbol de decisión para algo nuevo

1. **¿Puede hacerse cumplir en código?** → hazlo en código. Punto.
2. **¿Es una comprobación mecánica sobre una acción de Claude Code?** → hook.
3. **¿Es cierto en cualquier sesión de Claude Code, sin importar la tarea?** → `CLAUDE.md`.
4. **¿Es el procedimiento de una tarea concreta y repetible?** → un Skill.
5. **¿Gobierna qué puede hacer el bot por su cuenta?** → `AGENT_CAPABILITIES.md`.
6. **¿Es contexto para un humano?** → `docs/`.
7. **¿No encaja en ninguna?** → no va en la configuración de ningún agente.

Dos reglas transversales:

- **Un dato en vivo o una acción fuera de la conversación nunca va en prosa.** Va en
  una tool o en un cliente. Si te ves escribiendo un dato que caduca en un `.md`,
  estás en el sitio equivocado.
- **Prevención y detección no son intercambiables.** Un `allowedTools` impide el daño;
  un reviewer lo detecta después. Para lo irreversible hace falta lo primero.

---

## Checklist: añadir una tool nueva al bot

En el **mismo commit**:

- [ ] Entrada en `TOOLS` (`main.py`) con descripción y `input_schema`.
- [ ] Rama en `execute_tool()`.
- [ ] Fila en la tabla del nivel que le corresponda en `AGENT_CAPABILITIES.md`.
- [ ] Si es Nivel 3: la confirmación va **en código** (`requires_confirmation` + flag
      `force_*`), no en la descripción de la tool.
- [ ] Si escribe algo: ¿existe la tool simétrica para listarlo y deshacerlo?
- [ ] Si toca el sistema de archivos: ¿valida la ruta contra una raíz permitida?
- [ ] Tests en `tests/`.

## Checklist: añadir una regla a la configuración de un agente

- [ ] ¿Qué agente la va a leer, A o B? Ponla en su archivo.
- [ ] ¿Podría bajarse a código o a un hook? Si sí, bájala.
- [ ] ¿Describe algo que el código ya hace, o algo que quieres que haga? Si es lo
      segundo, no es una regla: es una tarea de backlog.
- [ ] ¿Está duplicada en otro sitio? Decide cuál es la fuente de verdad.
