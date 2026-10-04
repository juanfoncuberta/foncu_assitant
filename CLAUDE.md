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

## Ramas

- **Nunca se commitea, mergea ni hace push sobre `main`.** Todo cambio va en su
  propia rama, en cualquier sesión — no solo en las de `ejecutar_tarea_dev`.
- Antes del primer commit, crea la rama desde `main` actualizado:
  `git switch -c <prefijo>/<slug>`.
  - Prefijo según el tipo de cambio: `feat/`, `fix/`, `chore/` o `docs/`.
  - Slug en inglés, en minúsculas y con guiones, describiendo el cambio
    (ej. `chore/main-security-tests`, `fix/tool-name-invalid-chars`).
- Se hace push de la rama, nunca de `main`. Integrar en `main` (merge o PR) lo
  decide y lo hace el usuario.
- Si ya has commiteado en `main` por error y no hay push, mueve el commit a una
  rama nueva y deja `main` igual que `origin/main`; si ya hubo push, para y avisa.

Un hook `PreToolUse` sobre `Bash` (`.claude/hooks/block_commits_on_main.py`)
bloquea commits, merges, rebases y pushes sobre `main`. Si te bloquea, crea la
rama; no busques una vía alternativa.

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
- **Todo servicio externo va detrás de una interfaz y se obtiene por factory.**
  El resto del sistema habla con la interfaz, nunca con el proveedor concreto,
  para que cambiar o añadir uno sea escribir una clase y registrarla en
  `provider_factory.py`. Es el patrón que ya siguen las tareas
  (`TaskProvider` → `TodoistProvider`) y el consumo de IA
  (`UsageProvider` → `AnthropicApiUsageProvider`, `ClaudeCodeUsageProvider`).
- **Ningún nombre ata el sistema a un proveedor**: ni tools, ni tablas, ni
  columnas, ni funciones públicas (`consultar_gasto_ia`, no
  `consultar_gasto_claude`). El proveedor es un dato que se guarda, no parte
  del nombre.
- **La configuración vive en un único sitio**: `config.py`, que lee el entorno
  en cada llamada. Ningún módulo repite literales como la ruta de la base de
  datos, URLs o nombres de servicio; los pide a `config`. Si necesitas un valor
  nuevo, añádelo allí con su variable de entorno.
- **Los datos que cambian con el tiempo no se escriben en el código**: precios,
  tarifas, catálogos de modelos, límites comerciales, fechas de consulta y
  cualquier valor que dependa de un tercero o del negocio. Van en la base de
  datos, que es la fuente de verdad; un archivo de datos versionado (JSON) solo
  la siembra con lo que falte, sin pisar lo que ya haya; y se cambian sin tocar
  código (una tool del bot con confirmación de Nivel 3). Ejemplo:
  `model_prices.py` + `model_prices.json` + `actualizar_precio_ia`.
  Lo que sí puede ir en el código: constantes técnicas que solo cambian si
  cambia el propio código (timeouts, nombres de columnas, formatos).
  Los tests no dependen de los valores reales: siembran en la base de datos
  temporal los datos que necesitan.

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

### Antes de commitear en una sesión interactiva

Fuera de `ejecutar_tarea_dev` (que ya pasa por el `reviewer` dentro de su propio
flujo), ningún commit se hace sin este orden:

1. `pytest` completo en verde.
2. El subagent `reviewer` revisa el diff contra lo que pidió el usuario. Se le da
   el plan, no la explicación de cómo se hizo: si revisa el mismo agente que
   escribió el código, no hay revisión. En el encargo se le dice qué archivos
   protegidos aprobó tocar el usuario, para que no los cuente como bloqueantes.
3. Se corrigen los Bloqueantes y los "A corregir" cuya corrección cabe en el plan,
   y se vuelve al paso 1. No se presenta como terminado algo con Bloqueantes.
   Excepción: si una corrección se sale del plan, exige tocar un archivo protegido
   sin aprobación o el hallazgo parece un falso positivo, se para y se presenta al
   usuario tal cual. Nunca se aplica una corrección propuesta que salga del plan
   sin su confirmación (ver el incidente de `semantic_memory.py`).
4. Se presenta al usuario el resumen de los cambios y el informe del `reviewer`
   tal cual, sin filtrar los hallazgos.
5. Se commitea solo tras su confirmación explícita. Hasta entonces los cambios se
   quedan sin commitear en la rama de trabajo.

## Reintentos

Si un mismo comando o test falla dos veces con el mismo error, no lo intentes una
tercera vez. Detente y explica en tu resumen final qué fallaba, qué probaste y
cuál crees que es la causa. Nunca te quedes reintentando en bucle.

## Tareas lanzadas por `ejecutar_tarea_dev`

El procedimiento vive en `.claude/skills/ejecutar-tarea-dev/SKILL.md` y solo se
carga cuando toca. No lo dupliques aquí.
