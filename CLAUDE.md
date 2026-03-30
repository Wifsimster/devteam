# Devteam — Claude Code Instructions

Equipe de développeurs IA autonome pilotée par Discord. Un agent Python (aiohttp) reçoit des tâches via HTTP, lance Claude Code CLI en streaming, et diffuse les événements via WebSocket et Discord threads.

## Stack

- **Python 3.12** — aiohttp 3.9.5 (seule dépendance)
- **Claude Code CLI** — installé globalement via npm dans le container
- **Node.js 22** — requis pour Claude Code CLI
- **Docker** — container basé sur `node:22-slim`

## Structure du projet

```
agent.py          — Point d'entrée : serveur HTTP, WebSocket, orchestration Claude CLI
dashboard.html    — Dashboard web vanilla (HTML/JS/CSS) avec WebSocket temps réel
compose.yml       — Docker Compose (service dev-agents, réseau Traefik)
Dockerfile        — Build multi-stack (Node 22 + Python 3 + Claude CLI)
requirements.txt  — Dépendances Python (aiohttp)
prompts/ceo.md    — Prompt système du CEO Jarvis
.env              — Variables d'environnement (non versionné)
```

## Endpoints API

| Route | Méthode | Description |
|-------|---------|-------------|
| `/task` | POST | Soumet une tâche (channelId, content, messageId, author) |
| `/health` | GET | État du service (idle/busy + tâche en cours) |
| `/ws` | GET | WebSocket temps réel (événements, agents, timeline) |
| `/` | GET | Dashboard HTML |

## Commandes utiles

```bash
# Lancer en local (dev)
docker compose up -d --build

# Voir les logs
docker compose logs -f dev-agents

# Rebuild après modification
docker compose up -d --build --force-recreate
```

## Conventions

- **Langue du code :** anglais (variables, fonctions, commentaires)
- **Langue des messages Discord/UI :** français
- **Style Python :** fonctions async, pas de classes sauf `TaskState`
- **Pas de framework web lourd** — aiohttp suffit, pas de FastAPI/Flask
- **Dashboard vanilla** — pas de bundler, pas de framework JS
- **Un seul fichier Python** — toute la logique dans `agent.py`

## Architecture

- Le serveur accepte une seule tâche à la fois (`_task_lock`)
- Claude CLI est lancé en subprocess avec `--output-format stream-json`
- Le stream JSON est parsé ligne par ligne pour extraire les événements
- Les événements sont diffusés aux clients WebSocket et postés dans le thread Discord
- Les agents (Alice, Bob, Charlie) sont détectés via les tool_use `Agent` dans le stream

## Variables d'environnement

| Variable | Requis | Défaut | Description |
|----------|--------|--------|-------------|
| `DISCORD_BOT_TOKEN` | oui | — | Token bot Discord |
| `ANTHROPIC_API_KEY` | oui | — | Clé API Anthropic |
| `WORKSPACE` | non | `/workspace` | Répertoire des repos git |
| `CLAUDE_MODEL` | non | `claude-sonnet-4-6` | Modèle Claude à utiliser |
| `MAX_TURNS` | non | `50` | Nombre max de tours par tâche |

## Points d'attention

- Le container monte `/workspace` depuis le NAS — les repos sont partagés
- Timeout de 15 minutes par tâche (`asyncio.timeout(900)`)
- Les messages Discord sont découpés en chunks de 1950 caractères max
- Le dashboard reconnecte automatiquement le WebSocket toutes les 3 secondes
