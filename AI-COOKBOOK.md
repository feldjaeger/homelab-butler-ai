# 🍳 AI Homelab Cookbook

A practical guide to building an AI-managed homelab. From "I have a Proxmox cluster" to "my AI creates VMs and deploys services with one command."

This is not theory – it's the exact setup running [Homelab Pfannkuchen](https://github.com/feldjaeger/homelab-butler-ai), battle-tested through weeks of AI fails and fixes.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The AI Agent (Hermes)](#the-ai-agent)
3. [Choosing the Right LLM](#choosing-the-right-llm)
4. [The Butler API](#the-butler-api)
5. [Credential Management (Vaultwarden)](#credential-management)
6. [Ansible Automation](#ansible-automation)
7. [ISO Builder (Zero-Touch VMs)](#iso-builder)
8. [TTS / Voice Output](#tts-voice-output)
9. [Teaching Your AI (Skills & SOUL.md)](#teaching-your-ai)
10. [Adding New Service Endpoints](#adding-new-service-endpoints)
11. [Lessons Learned (What Went Wrong)](#lessons-learned)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  User (Telegram / CLI / Web)                            │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│  AI Agent (Hermes)                                      │
│  - LLM: qwen3.5:397b via Ollama Cloud                  │
│  - SOUL.md: personality + rules                         │
│  - Skills: task-specific knowledge                      │
│  - MCP: Vaultwarden, Dockhand                          │
└──────────────┬──────────────────────────────────────────┘
               │ HTTP (single Bearer token)
┌──────────────▼──────────────────────────────────────────┐
│  Butler API (FastAPI, port 8888)                        │
│  ┌─────────────┬──────────────┬───────────┬──────────┐ │
│  │ Service     │ VM Lifecycle │ Ansible/  │ TTS      │ │
│  │ Proxy       │ (Proxmox)   │ Inventory │ (Speaker)│ │
│  │ 15 backends │ create/list  │ add host  │ speak    │ │
│  │ auto-auth   │ delete/status│ run play  │ telegram │ │
│  └──────┬──────┴──────┬───────┴─────┬─────┴────┬─────┘ │
└─────────┼─────────────┼─────────────┼──────────┼───────┘
          │             │             │          │
    ┌─────▼─────┐ ┌─────▼─────┐ ┌────▼────┐ ┌──▼──┐
    │ Sonarr    │ │ Proxmox   │ │Ansible  │ │ Pi5 │
    │ Radarr    │ │ Cluster   │ │on auto- │ │ TTS │
    │ Grafana   │ │ (7 nodes) │ │mation1  │ │     │
    │ n8n ...   │ │           │ │         │ │     │
    └───────────┘ └───────────┘ └─────────┘ └─────┘
```

---

## The AI Agent

We use [Hermes](https://github.com/lks-ai/hermes) – an open-source AI agent framework that connects LLMs to tools (terminal, browser, MCP servers, skills).

### Installation

```bash
pip install hermes-ai
hermes init
```

### Key Config (`~/.hermes/config.yaml`)

```yaml
model:
  default: qwen3.5:397b-cloud    # The LLM
  provider: ollama-cloud          # Via Ollama Cloud (no local GPU needed)

agent:
  max_turns: 90
  auto_approve: true              # Don't ask for confirmation on every tool call
  yolo: true                      # Skip safety prompts (for homelab use)

mcp_servers:
  vaultwarden:                    # Credential access via MCP
    command: node
    args: ["/path/to/mcp-vaultwarden-server/server.js"]
    env:
      BITWARDEN_HOST: https://vault.example.com
      BW_CLIENTID: user.xxx
      BW_CLIENTSECRET: xxx
      BW_MASTER_PASSWORD: xxx
```

### Running as a Service (Telegram Bot)

```ini
# ~/.config/systemd/user/hermes-gateway.service
[Unit]
Description=Hermes Agent Gateway
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash -c 'set -a; source /home/user/.hermes/.env; set +a; exec hermes gateway run'
Restart=always
RestartSec=5
Environment=HOME=/home/user

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now hermes-gateway
```

The `.env` file contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS`.

---

## Choosing the Right LLM

**This is the single most important decision.** We tested multiple models:

| Model | Type | Tool-Calling | Agent Reliability |
|-------|------|-------------|-------------------|
| `qwen3-coder:480b` | Coding | ❌ Terrible | Hallucinated success, never made actual API calls |
| `qwen3.5:397b` | General | ✅ Excellent | Follows instructions, makes real HTTP calls |
| `minimax-m2.7` | Agent | ✅ Good | 97% skill adherence per benchmarks |
| `llama3.3:70b` | General | ✅ Good | Solid but slower reasoning |

### Key Insight

**Coding models ≠ Agent models.** A model optimized for code generation will try to *write* a script instead of *executing* an API call. You need a general-purpose model with native tool-calling support.

### Ollama Cloud

We run models via [Ollama Cloud](https://ollama.com/pricing) ($20/mo Pro plan). The AI agent VM has only 16GB RAM – the LLM runs in Ollama's datacenter.

```bash
ollama pull qwen3.5:397b-cloud
hermes config set model.default "qwen3.5:397b-cloud"
```

---

## The Butler API

See [app.py](./app.py) for the full implementation. The core idea:

**One Bearer token → access to everything.** The AI doesn't need to know 15 different API keys. It authenticates once with Butler, and Butler handles per-service auth.

### Adding a Service

```python
SERVICES = {
    "sonarr": {
        "url": "http://10.0.0.1:8989",
        "auth": "apikey",           # apikey | bearer | session | proxmox | n8n
        "vault_key": "sonarr-key",  # name in Vaultwarden
        "key_file": "sonarr",       # fallback flat file
    },
}
```

### Auth Types

| Type | What Butler Does |
|------|-----------------|
| `apikey` | Adds `X-Api-Key: <key>` header |
| `bearer` | Adds `Authorization: Bearer <key>` header |
| `n8n` | Adds `X-N8N-API-KEY: <key>` header |
| `proxmox` | Adds `PVEAPIToken=<tokenid>=<secret>` header |
| `session` | Logs in via POST, stores session cookie, auto-refreshes |

### Catch-All Proxy

Any request to `/{service}/{path}` gets proxied to the backend with credentials injected. The AI just does:

```bash
curl http://butler:8888/sonarr/api/v3/series
```

And gets back Sonarr's response, fully authenticated.

---

## Credential Management

### Why Vaultwarden (not Infisical, not HashiCorp Vault)

We tried Infisical first. It was:
- Complex to set up (Postgres + Redis + multiple containers)
- Overkill for a homelab (enterprise features we don't need)
- Hard for the AI to query (complex API)

**Vaultwarden** (Bitwarden-compatible) is:
- Single container, SQLite
- Already used for personal passwords
- Has a CLI (`bw`) and MCP server

### Two-Layer Credential Access

```
Layer 1: MCP (AI reads secrets directly)
  AI → Vaultwarden MCP Server → Vault API → Secret value
  Used for: one-off lookups, debugging

Layer 2: Butler Cache (API proxy uses cached secrets)
  Cron (every 30 min) → bw CLI → flat files → Butler reads on startup
  Used for: all proxied API calls (fast, no vault dependency at runtime)
```

### Vault Sync Script

```bash
#!/bin/bash
# Runs via cron: */30 * * * * /path/to/vault-sync.sh
SESSION=$(bw unlock --passwordenv BW_PASSWORD --raw)
bw sync --session "$SESSION"
bw list items --session "$SESSION" | python3 -c "
import sys, json, os
items = json.load(sys.stdin)
for item in items:
    name = item.get('name', '').lower().replace(' ', '-')
    notes = item.get('notes') or ''
    if name and notes:
        with open(f'/cache/{name}', 'w') as f:
            f.write(notes.strip())
"
```

### MCP Server Setup

```bash
npm install -g mcp-vaultwarden-server
```

Add to Hermes config:
```yaml
mcp_servers:
  vaultwarden:
    command: node
    args: ["/path/to/mcp-vaultwarden-server/server.js"]
    env:
      BITWARDEN_HOST: https://vault.example.com
      BW_CLIENTID: user.xxx
      BW_CLIENTSECRET: xxx
      BW_MASTER_PASSWORD: xxx
```

Now the AI can do: "What's the Sonarr API key?" and get it from Vaultwarden.

---

## Ansible Automation

### Structure

```
ansible/
├── pfannkuchen.sh          # Wrapper script (human-friendly)
├── pfannkuchen.ini          # Inventory (hosts + groups)
├── site.yml                 # Full setup playbook
├── iso-builder/             # Custom ISO builder
├── roles/
│   ├── base/                # SSH keys, packages, locale
│   ├── docker/              # Docker CE + compose
│   ├── borg/                # Borgmatic backup to Hetzner StorageBox
│   ├── hawser/              # Dockhand agent (Docker management)
│   ├── sysctl/              # Network tuning
│   ├── nvidia/              # GPU drivers + container toolkit
│   ├── wireguard/           # WireGuard VPN
│   ├── telegraf/            # Monitoring agent
│   └── ...
├── group_vars/              # Encrypted vars (sops + age)
└── host_vars/               # Per-host overrides
```

### The Wrapper Script

```bash
# Full VM setup (base + docker + borg + hawser + sysctl)
./pfannkuchen.sh setup my-hostname

# Just GPU drivers
./pfannkuchen.sh gpu my-hostname

# Update all hosts
./pfannkuchen.sh update
```

### Butler Integration

Butler calls Ansible via SSH to the automation host:

```python
def _ssh(host, cmd, timeout=600):
    r = subprocess.run(["ssh", host, cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

# In /vm/create endpoint:
_ssh("user@automation", "cd /ansible && ./pfannkuchen.sh setup hostname", timeout=600)
```

---

## ISO Builder

See [iso-builder/](./iso-builder/) for the full scripts.

### What It Does

1. Downloads Debian netinst ISO
2. Extracts it
3. Injects a preseed config (static IP, SSH key, user, packages)
4. Patches GRUB + isolinux for zero-touch boot
5. Rebuilds the ISO
6. Uploads to Proxmox node
7. Creates VM via `pvesh` API
8. Starts VM → Debian installs unattended → SSH ready in ~5 min

### Why Custom ISOs?

Cloud-init doesn't work well with Proxmox VMs (no cloud-init datasource without extra setup). Preseed is native Debian and just works.

---

## TTS / Voice Output

### Architecture

```
Butler /tts/speak → Chatterbox TTS (GPU) → WAV
                                              ↓
                          target=speaker → Pi5 plays audio
                          target=telegram → OGG → Telegram voice message
```

### Pi Speaker Service

A simple Python HTTP server on a Raspberry Pi with a speaker attached:

```python
# Receives text, calls Chatterbox, plays audio via pw-play
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        text = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["text"]
        wav = urllib.request.urlopen(Request(CHATTERBOX_URL, data=json.dumps({
            "text": text, "voice_mode": "clone",
            "reference_audio_filename": "my-voice.mp3",
        }).encode())).read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
        subprocess.run(["pw-play", f.name])
```

---

## Teaching Your AI

### SOUL.md (Personality + Rules)

```markdown
# Trulla 🍳 – Homelab Assistant

## Butler API (your main tool)
Base: http://butler:8888
Auth: `Authorization: Bearer TOKEN`

### Quick Reference
- `GET /vm/list` – all VMs
- `POST /vm/create` – create VM (takes ~10 min!)
- `POST /tts/speak` – text to speech
- `/{service}/{path}` – proxy to any service

## Rules
- ALWAYS use Butler API, NEVER SSH directly
- ALWAYS VMs, NEVER LXC
- NEVER touch Caddy, Emby, FRP without asking
```

### Skills (Task-Specific Knowledge)

Skills are markdown files in `~/.hermes/skills/` that the AI loads when relevant:

```markdown
---
name: tts-voice
description: Text-to-speech via Butler API
tags: [tts, voice, homelab]
---

# TTS Voice Skill

## Speaker output
curl -X POST http://butler:8888/tts/speak \
  -H "Authorization: Bearer TOKEN" \
  -d '{"text": "Hello!", "target": "speaker"}'

## Telegram voice message
curl -X POST http://butler:8888/tts/speak \
  -d '{"text": "Hello!", "target": "telegram"}'
# Then respond with: MEDIA:/tmp/trulla_voice.ogg
```

---

## Adding New Service Endpoints

Want your AI to control a new service? Three steps:

### 1. Add to Butler SERVICES dict

```python
"my-service": {
    "url": "http://10.0.0.5:3000",
    "auth": "bearer",
    "vault_key": "my-service-token",
},
```

### 2. Store the credential

```bash
# Option A: Flat file
echo "my-api-token" > /data/api/my-service

# Option B: Vaultwarden
# Create item named "my-service-token" with the API key in Notes
```

### 3. Tell the AI

Add to SOUL.md or create a skill:
```markdown
## My Service
- List items: GET /my-service/api/items
- Create item: POST /my-service/api/items {"name": "..."}
```

That's it. The AI can now `curl http://butler:8888/my-service/api/items` and it works.

### Common Services to Add

| Service | Auth Type | API Docs |
|---------|-----------|----------|
| Sonarr/Radarr | `apikey` | `/api/v3/series`, `/api/v3/movie` |
| Grafana | `bearer` | `/api/dashboards`, `/api/datasources` |
| Home Assistant | `bearer` | `/api/states`, `/api/services/{domain}/{service}` |
| Proxmox | `proxmox` | `/api2/json/nodes`, `/api2/json/cluster` |
| n8n | `n8n` | `/api/v1/workflows`, `/api/v1/executions` |
| Outline | `bearer` | `/api/documents.search`, `/api/documents.create` |
| Forgejo/Gitea | `bearer` | `/api/v1/repos`, `/api/v1/user/repos` |
| Uptime Kuma | `bearer` | Limited API – mostly web UI |

---

## Lessons Learned

### 1. Coding LLMs Can't Do Agent Work
`qwen3-coder:480b` would write a Python script to call the API instead of actually calling it. Then it would say "Done!" without executing anything. Switch to a general-purpose model.

### 2. Sub-Agent Delegation Doesn't Work (Yet)
We tried having the main AI delegate tasks to sub-agents. The sub-agents hallucinated even worse. Disabled delegation entirely – the main AI does everything itself.

### 3. FastAPI Catch-All Routes Are Greedy
`/{service}/{path:path}` matches EVERYTHING, including your specific routes like `/vm/list`. Fix: add a `SKIP_SERVICES` set at the top of the catch-all handler.

### 4. Docker Containers Don't Have SSH
When Butler needs to SSH to other hosts for VM creation, the `python:slim` image doesn't include `openssh-client`. Add it to the Dockerfile.

### 5. Docker Volumes Die with `down -v`
Named volumes (`database:`) get deleted by `docker compose down -v`. Use bind mounts (`/app-config/db:/var/lib/postgresql/data`) for anything you want to keep and backup.

### 6. Vaultwarden MCP + dotenv v17 = Broken
`dotenv` v17 writes to stdout on load, which breaks MCP's JSON-RPC protocol. Pin to v16: `npm install dotenv@16`.

### 7. The AI Will Break Your Network
Our AI once created a VM with the same IP as the host it was running on. Add explicit rules to SOUL.md about what NOT to do.

### 8. Keep SOUL.md Small
The AI's system prompt has limited space. Don't dump your entire infrastructure docs in there. Use skills for detailed knowledge, SOUL.md for rules and quick reference only.

---

## License

MIT – take it, adapt it, make your homelab smarter.

Built by [@feldjaeger](https://github.com/feldjaeger) with Kiro 🤖 and Trulla 🍳.
