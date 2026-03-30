# Devteam

Equipe de developpeurs IA autonome orchestree par un agent CEO (Jarvis). Pilotable depuis Discord `#dev` avec dashboard temps reel.

## Architecture

```
Discord #dev → discord-bridge → POST /task → Claude Code CLI (CEO Jarvis)
                                                ├── Agent "Alice" (Frontend)
                                                ├── Agent "Bob" (Backend)
                                                └── Agent "Charlie" (DevOps)

Events → Discord thread (mobile) + Dashboard websocket (desktop)
```

## Stack

- **Runtime** : Claude Code CLI (Sonnet 4.6) — tool-calling natif (Read, Write, Edit, Bash, Git, Agent)
- **Orchestration** : Agent CEO delegue aux sous-agents avec worktrees git isoles
- **Backend** : Python aiohttp (HTTP server + WebSocket)
- **Dashboard** : HTML/JS vanilla, websocket temps reel
- **Container** : Node.js 22 + Python 3.12 + Claude Code CLI + git

## Services

| Endpoint | Role |
|----------|------|
| `POST /task` | Recoit une tache depuis discord-bridge, lance Claude CLI en background |
| `GET /health` | Status JSON (`idle` / `busy` + tache en cours) |
| `GET /ws` | WebSocket temps reel (events, agents, timeline) |
| `GET /` | Dashboard web |

## Discord

- **Canal** : `#dev` — envoie un message, Jarvis prend le relais
- **Thread** : cree automatiquement par tache, updates en temps reel
- **Reaction** : 🚀 = tache acceptee

## Deploiement

```bash
# Creer le .env
cat > .env <<EOF
DISCORD_BOT_TOKEN=<token>
ANTHROPIC_API_KEY=<key>
DOMAIN=battistella.ovh
WORKSPACE=/workspace
CLAUDE_MODEL=claude-sonnet-4-6
MAX_TURNS=50
EOF

# Lancer
docker compose up -d --build
```

Le dashboard est accessible sur `https://devteam.<DOMAIN>/`.

## Workspace

Les repos git a travailler sont montes dans `/workspace` (NAS Unraid via NFS). Chaque sous-repertoire est un repo independant. Les agents les detectent automatiquement.

## Personnalites

Les personnalites des agents sont definies dans `prompts/` et dans le `CLAUDE.md` du workspace :

| Agent | Role | Personnalite |
|-------|------|-------------|
| **Jarvis** (CEO) | Orchestration, decomposition, review, merge | Strategique, decisif, autonome |
| **Alice** | Frontend (React, Next.js, Tailwind) | Perfectionniste UX, mobile-first |
| **Bob** | Backend (Node.js, Python, PostgreSQL) | Rigoureux, securite, simplicite |
| **Charlie** | DevOps (Docker, CI/CD, infra) | Pragmatique, minimaliste |
