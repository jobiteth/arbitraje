# AGENTS.md

Este repositorio usa un único fichero de instrucciones canónico:
[`CLAUDE.md`](./CLAUDE.md).

Cualquier agente (Claude, DeepSeek, ChatGPT, Gemini u otro) debe leer `CLAUDE.md`
completo antes de generar código. Este archivo existe sólo como alias de
compatibilidad para herramientas que buscan `AGENTS.md`.

Resumen de las reglas no negociables:

- La IA propone, el usuario decide. Nada con efectos sin confirmación explícita.
- La aplicación **no firma ni emite** transacciones.
- Secretos sólo en el keyring del sistema; nunca en `config.toml` ni en logs.
- Comentarios y documentación en español; identificadores en inglés.
- Antes de dar una tarea por hecha: `ruff`, `mypy --strict` y `pytest` en verde.
