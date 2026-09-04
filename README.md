# Asistente personal — Milestone 1: Telegram + Claude

Este primer paso solo conecta Telegram con Claude. Todavía no toca Todoist —
eso lo añadimos en el siguiente milestone una vez esto funcione.

## 1. Crear el bot en Telegram

1. Abre un chat con **@BotFather** en Telegram.
2. Envía `/newbot` y sigue las instrucciones (nombre + username).
3. Te da un token tipo `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Guárdalo.

## 2. Configurar el proyecto

```bash
cd telegram-claude-bot
python3 -m venv venv
source venv/bin/activate      # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edita `.env` y pega tu `TELEGRAM_BOT_TOKEN` y tu `ANTHROPIC_API_KEY`
(la sacas en console.anthropic.com → API Keys).

## 3. Arrancar

```bash
python main.py
```

Abre Telegram, busca tu bot por el username que le pusiste, y mándale
un mensaje. Debería responderte usando Claude.

## 4. (Opcional) Probar con Topics

Si quieres probar ya la idea de agrupar por proyecto:

1. Crea un grupo en Telegram.
2. Entra en la configuración del grupo → activa **Temas** (Topics).
3. Añade tu bot al grupo.
4. Crea un par de topics (por ejemplo "Cuoco" y "Personal") y escríbele
   al bot dentro de cada uno — verás en los logs (`thread=...`) que cada
   topic tiene un id distinto. Esa es la pieza que usaremos para separar
   el contexto por proyecto en el siguiente milestone.

## Siguiente paso

Conectar Todoist: guardar qué `thread_id` corresponde a qué proyecto,
y darle a Claude funciones (tool use) para crear/listar/priorizar tareas.
