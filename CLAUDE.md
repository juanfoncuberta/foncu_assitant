# CLAUDE.md

Reglas permanentes para Claude Code trabajando en este repositorio.
Aplican en cualquier sesión, sea cual sea la tarea.

Si necesitas saber **dónde** va una regla nueva, o cómo se relacionan el bot y
Claude Code, lee `docs/agent-architecture.md` antes de escribir nada aquí.

## Commit messages

- **Asunto (primera línea del mensaje)**: máximo 72 caracteres, en inglés, modo
  imperativo y en minúsculas. Una sola frase que resuma el cambio
  (ej. `add tests for claude_code_executor edge cases`, nunca una lista de módulos).
- **Cuerpo**: solo cuando aporte algo que el diff no muestra — el *porqué* de una
  decisión no obvia. La mayoría de commits no necesitan cuerpo.
- Nunca enumeres módulo por módulo qué cambió; eso ya se ve en `git diff` / `git show`.
- Sin firmas de ningún tipo (`Co-authored-by`, `Generated with`, etc.). Ya están
  desactivadas en `.claude/settings.json`; no las añadas a mano.

## Reglas de arquitectura (no negociables)

- **Nunca reescribas lógica de producción existente** para facilitar su testeo,
  ni por ningún otro motivo que no se haya pedido explícitamente. Si detectas
  que hace falta un cambio así, repórtalo como hallazgo en tu resumen final —
  no lo arregles por tu cuenta.
  (Motivado por un incidente real: al pedir solo tests para `semantic_memory.py`,
  se reescribió el propio módulo de producción sin que nadie lo pidiera.)
- Los controllers/handlers (`main.py`) no acceden a la base de datos
  directamente — pasan por los módulos `*_client.py` / `*_provider.py`.
- Los secretos viven solo en `.env`, nunca en código ni en commits.

## Archivos protegidos

Nunca edites estos archivos, ni siquiera como paso intermedio, sin aprobación
humana explícita:

`.env` · `docker-compose.yml` · `Dockerfile` · `deploy.sh` · cualquier archivo
bajo `.claude/` · cualquier migración de base de datos.

Un hook `PreToolUse` bloquea las escrituras sobre estas rutas, así que un intento
fallará con un error. Si una tarea parece necesitar tocarlas, detente y repórtalo
en tu resumen final en vez de buscar una vía alternativa.

## Tests

Todo cambio en lógica de negocio lleva su test **en el mismo commit**. Que la suite
pase en verde no basta: una suite sin tests de lo que acabas de tocar pasa
perfectamente y no prueba nada.

Concretamente, llevan test obligatorio:

- Una tool nueva o un cambio en su comportamiento (`execute_tool` en `main.py`).
- Cualquier validación, confirmación o límite — es decir, todo lo que exista para
  impedir algo.
- Cualquier corrección de bug: primero el test que lo reproduce, luego el arreglo.

No lo llevan: cambios de documentación, de formato, o de comentarios.

Si un módulo no se puede testear sin montar medio sistema, eso es el hallazgo —
repórtalo en tu resumen final en vez de saltarte el test.

## Verificación obligatoria

Una tarea no está completa hasta que:

1. `pytest` pasa en su totalidad (no solo los tests nuevos que haya añadido la tarea).
   El hook `PostToolUse` ya ejecuta la suite rápida tras cada edición; basta con
   leer su salida, no hace falta relanzarla a mano salvo que quieras incluir los
   tests marcados como `integration`.
2. Cualquier archivo modificado fuera del alcance declarado para la tarea se
   reporta explícitamente — nunca se pasa por alto en silencio.

Si algo de esto falla, la tarea se reporta como fallida con el motivo — nunca
como "hecho" a medias.

## Reintentos

Si un mismo comando o test falla dos veces con el mismo error, no lo intentes una
tercera vez. Detente y explica en tu resumen final qué fallaba, qué probaste y
cuál crees que es la causa. Nunca te quedes reintentando en bucle.

## Tareas lanzadas por `ejecutar_tarea_dev`

El procedimiento vive en `.claude/skills/ejecutar-tarea-dev/SKILL.md` y solo se
carga cuando toca. No lo dupliques aquí.
