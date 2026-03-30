# Devteam — Agent Instructions

Equipe de développeurs IA autonome. Un serveur Python aiohttp reçoit des tâches Discord, lance Claude Code CLI en streaming, et diffuse les résultats via WebSocket et threads Discord.

## Structure

- `agent.py` — Serveur HTTP + WebSocket + orchestration Claude CLI (fichier unique)
- `dashboard.html` — Dashboard web vanilla (HTML/JS/CSS)
- `compose.yml` — Docker Compose avec Traefik
- `Dockerfile` — Node 22 + Python 3 + Claude CLI
- `requirements.txt` — aiohttp uniquement
- `prompts/ceo.md` — Prompt système CEO

## Commandes

```bash
# Build et lancer
docker compose up -d --build

# Logs
docker compose logs -f dev-agents

# Rebuild
docker compose up -d --build --force-recreate
```

## Conventions à respecter

- Code en **anglais**, messages utilisateur en **français**
- Toute la logique serveur reste dans **un seul fichier** (`agent.py`)
- Dashboard **vanilla** — pas de framework JS ni bundler
- **aiohttp** uniquement — pas de FastAPI/Flask
- Commits atomiques, messages en anglais
- Jamais de commit direct sur `main` — utiliser des branches `feat/`

## Points critiques

- Une seule tâche à la fois (verrou `_task_lock`)
- Timeout 15 min par tâche
- Messages Discord découpés à 1950 caractères
- Le container monte `/workspace` (NAS) — ne pas supprimer de repos
- Variables d'environnement sensibles dans `.env` (non versionné)
