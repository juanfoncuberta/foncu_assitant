---
name: ejecutar-tarea-dev
description: Procedimiento para implementar una tarea de desarrollo lanzada por ejecutar_tarea_dev (claude_code_executor.py) en modo headless. Úsalo cuando el prompt sea una tarea a implementar en este repo que termina pidiendo commitear los cambios.
---

# Ejecutar una tarea de desarrollo

Te ha lanzado `claude_code_executor.py` con `claude -p`. Nadie está mirando:
no puedes preguntar, solo informar en tu resumen final.

## Lo que ya ha hecho el executor — no lo repitas

- Comprobó que el repo estaba limpio.
- Creó una rama nueva (`feat/`, `fix/` o `chore/`) y ya estás en ella.
- Al terminar: volverá a la rama original, commiteará lo que dejes sin
  commitear y pasará tu diff al subagent `reviewer`.

## Pasos

1. Antes de editar nada, escribe el alcance: qué archivos vas a tocar y qué
   cambias en cada uno.
2. Cíñete a esos archivos. Si se queda corto, para y repórtalo; no amplíes
   el alcance por tu cuenta.
3. Tests según `CLAUDE.md`. El hook PostToolUse lanza la suite rápida tras
   cada edición: lee su salida.
4. Commitea con el formato de `CLAUDE.md` (`git add -A` + `git commit -m`).
5. Resumen final: qué problema había, qué cambiaste y por qué lo resuelve;
   cualquier archivo tocado fuera del alcance del paso 1; cualquier cosa que
   viste rota y no arreglaste.

## Según el tipo de tarea

| Tipo | Qué hacer |
|---|---|
| Consulta (sin cambios de código) | No edites nada. Responde y termina |
| Cambio acotado (caso normal) | Pasos 1–5. `Bash` solo para pytest, lint y git |
| Cambio estructural (varios módulos) | Igual, pero si el alcance se queda corto, para antes de ampliarlo |

## Nunca

- Cambiar de rama, crear otra, hacer merge, push, reset o stash.
- Tocar archivos protegidos (ver `CLAUDE.md`).
- Dar la tarea por completada en Todoist: esa decisión es del usuario.
