# CLAUDE.md

## Commit messages

- **Subject line**: ≤ ~72 characters; one imperative phrase summarising the change
  (e.g. `add tests for claude_code_executor edge cases`, not a list of modules).
- **Body**: only when it adds something the diff cannot show — the *why* behind
  a non-obvious decision. Most commits need no body.
- Never list module-by-module what changed; that is already visible in
  `git diff` / `git show`.

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

## Verificación obligatoria

Una tarea no está completa hasta que:

1. `pytest` pasa en su totalidad (no solo los tests nuevos que haya añadido la tarea).
2. Cualquier archivo modificado fuera del alcance declarado para la tarea se
   reporta explícitamente — nunca se pasa por alto en silencio.

Si algo de esto falla, la tarea se reporta como fallida con el motivo — nunca
como "hecho" a medias.

## Límite de reintentos

Máximo 2 reintentos automáticos por tarea. Si el mismo tipo de fallo se repite
(mismo error, mismo archivo), se detiene y se reporta — nunca se queda
reintentando en bucle.
