FROM python:3.12-slim

WORKDIR /app

# Install Node.js 20.x LTS (required by Claude Code CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# requirements-dev.txt (pytest) tambien: Claude Code ejecuta los tests DENTRO de este
# contenedor, tanto el hook PostToolUse como las tareas de ejecutar_tarea_dev.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY *.py .
COPY AGENT_CAPABILITIES.md .
# Datos, no codigo: siembra la tabla model_prices al arrancar (ver model_prices.py).
COPY model_prices.json .

RUN useradd -m botuser && chown -R botuser:botuser /app \
    && su botuser -c "git config --global user.email 'juan.foncuberta@gmail.com'" \
    && su botuser -c "git config --global user.name 'Juan Foncuberta'"
USER botuser

CMD ["python", "main.py"]
