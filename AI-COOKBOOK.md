# 🍳 AI Homelab Cookbook

A practical guide to building an AI-managed homelab. From "I have a Proxmox cluster" to "my AI creates VMs and deploys services with one command."

This is not theory – it's the exact setup running [Homelab Pfannkuchen](https://github.com/feldjaeger/homelab-butler-ai), battle-tested through weeks of AI fails and fixes.

> **Butler API Version:** 2.1.0  
> **Tested Models:** qwen3.5:397b-cloud ✅, minimax-m2.7 ✅  
> **Failed Models:** qwen3-coder:480b ❌ (hallucinates success)

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The AI Agent (Hermes/Trulla)](#the-ai-agent)
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
│  AI Agent (Hermes/Trulla)                               │
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

### Network Layout (Homelab Pfannkuchen)

| Network | Subnet | Purpose |
|---------|--------|---------|
| Management | 10.5.85.0/24 | Proxmox Nodes, automation1 |
| VM Subnets | 10.X.1.0/24 | Per Node (X = Node-Nummer) |
| WireGuard VPN | 10.200.200.0/24 | All Nodes ↔ VPS (MTU 1340) |
| VPS External | 159.69.245.190 | Caddy Reverse Proxy |

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

### Self-Discovery

The AI should **always** start with:

```bash
curl http://butler:8888/
```

Response includes:
- All configured services with URLs and auth types
- All available endpoints
- Current vault item count
- Service health status

### Endpoint Categories

#### 1. Service Proxy

```
GET/POST/PUT/DELETE /{service}/{path}
```

Butler automatically injects the correct auth header based on service config:

| Auth Type | Header | Example Services |
|-----------|--------|------------------|
| `apikey` | `X-Api-Key: <key>` | Sonarr, Radarr, Seerr, WAHA |
| `bearer` | `Authorization: Bearer <token>` | Outline, Grafana, Home Assistant |
| `n8n` | `X-N8N-API-KEY: <key>` | n8n |
| `proxmox` | `PVEAPIToken=<user>=<secret>` | Proxmox VE |
| `session` | Cookie (auto-managed) | Dockhand |

#### 2. VM Lifecycle

```bash
# Create a complete VM with one call
curl -X POST http://butler:8888/vm/create \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "node": 5,
    "ip": "10.5.1.115",
    "hostname": "my-service",
    "cores": 2,
    "memory": 4096,
    "disk": 32
  }'
```

**What happens automatically:**
1. Builds custom Debian ISO with preseed (static IP, SSH keys, user)
2. Uploads ISO to Proxmox node via `pvesh`
3. Creates VM with EFI boot, SCSI disk, virtio NIC
4. Starts VM and waits for SSH (~5 min)
5. Adds host to Ansible inventory
6. Runs Ansible base setup (Docker, Borgmatic backups, monitoring agent)

**Response:**
```json
{
  "status": "ok",
  "hostname": "my-service",
  "ip": "10.5.1.115",
  "vmid": 123,
  "steps": [
    "iso-builder: ok",
    "ssh: my-service reachable",
    "inventory: added",
    "ansible: ok"
  ]
}
```

⚠️ **Timeout:** Set to **700s** – VM creation takes ~10 minutes!

#### 3. TTS (Text-to-Speech)

```bash
# Speaker output (Raspberry Pi with speaker)
curl -X POST http://butler:8888/tts/speak \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"text": "VM erstellt!", "target": "speaker"}'

# Telegram voice message
curl -X POST http://butler:8888/tts/speak \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"text": "Backup fertig!", "target": "telegram"}'
```

---

## Credential Management

### Option A: Vaultwarden (Recommended)

Butler syncs credentials from Vaultwarden to a local cache directory:

```bash
# On host (cron job)
./vault-sync.sh  # Writes to /data/vault-cache/
```

**Vault Item Structure:**
- **Name:** `sonarr-key` (matches `vault_key` in butler.yaml)
- **Type:** Secure Note
- **Content:** Raw API key (no JSON, no extra text)

### Option B: Flat Files

For quick setup or migration:

```bash
mkdir -p /data/api
echo "your-api-key-here" > /data/api/sonarr
chmod 600 /data/api/sonarr
```

### Credential Loading Order

1. Vault cache (`/data/vault-cache/<name>`)
2. Flat file (`/data/api/<name>`)

This allows gradual migration from flat files to Vaultwarden.

---

## Ansible Automation

### Directory Structure

```
/opt/ansible/
├── inventory.ini          # Auto-populated by Butler
├── group_vars/
│   └── all.yml           # Common vars (Docker version, etc.)
├── host_vars/
│   └── my-vm.yml         # Host-specific vars
└── playbooks/
    ├── base-setup.yml    # Docker, users, SSH keys
    ├── borgmatic.yml     # Backup configuration
    └── monitoring.yml    # Telegraf, Prometheus exporters
```

### Base Setup Playbook

```yaml
# playbooks/base-setup.yml
- hosts: all
  become: yes
  tasks:
    - name: Install Docker
      apt:
        name: docker.io
        state: present
    
    - name: Add user to docker group
      user:
        name: "{{ ansible_user }}"
        groups: docker
        append: yes
    
    - name: Deploy SSH keys
      authorized_key:
        user: "{{ ansible_user }}"
        key: "{{ lookup('file', '~/.ssh/id_rsa.pub') }}"
    
    - name: Install borgmatic
      apt:
        name: borgmatic
        state: present
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
2. Extracts it with `xorriso`
3. Injects preseed config:
   - Static IP configuration
   - SSH public key
   - User account creation
   - Package selection (Docker, borgmatic, etc.)
4. Patches GRUB + isolinux for zero-touch boot
5. Rebuilds the ISO
6. Uploads to Proxmox node via `pvesh`
7. Creates VM via Proxmox API
8. Starts VM → Debian installs unattended → SSH ready in ~5 min

### Preseed Example

```preseed
# Preseed configuration for zero-touch Debian install
d-i netcfg/choose_interface select eth0
d-i netcfg/get_ipaddress string 10.5.1.115
d-i netcfg/get_netmask string 255.255.255.0
d-i netcfg/get_gateway string 10.5.1.1
d-i netcfg/get_nameservers string 1.1.1.1 8.8.8.8

d-i passwd/user-fullname string Homelab VM
d-i passwd/username string sascha
d-i passwd/user-password password changeme
d-i passwd/user-password-again password changeme

d-i pkgsel/include string openssh-server docker.io borgmatic
d-i pkgsel/upgrade select none

# SSH key injection
d-i preseed/late_command string \
  echo "ssh-ed25519 AAAA..." >> /target/home/sascha/.ssh/authorized_keys
```

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
        text = json.loads(self.rfile.read(int(self.headers["Content-Length"]))["text"]
        wav = urllib.request.urlopen(Request(CHATTERBOX_URL, data=json.dumps({
            "text": text, "voice_mode": "clone",
            "reference_audio_filename": "my-voice.mp3",
        }).encode())).read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
        subprocess.run(["pw-play", f.name])
```

### Chatterbox TTS Server

```bash
# On GPU host
curl -X POST http://gpu-host:8004/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello World",
    "voice_mode": "clone",
    "reference_audio_filename": "deep_thought.mp3"
  }' \
  --output voice.wav
```

---

## Teaching Your AI

### SOUL.md (Personality + Rules)

With Butler v2.1, the AI can self-discover endpoints via `GET /`. But you still need SOUL.md for rules and personality:

```markdown
# Trulla 🍳 – Homelab Assistant

## Butler API (your main tool)
Base: http://butler:8888
Auth: `Authorization: Bearer YOUR_TOKEN`

## Self-Discovery
Call `GET /` to see all available services and endpoints.
Call `GET /docs` for interactive Swagger UI.

## Rules
- ALWAYS use Butler API, NEVER SSH directly to services
- ALWAYS VMs, NEVER LXC
- NEVER touch Caddy, Emby, FRP without asking
- Use `?dry_run=true` before creating VMs if unsure
- IP schema: 10.X.1.Y (X = Node number)
- Node 7 VMs: SSH user is `chris@`, not `sascha@`
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
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"text": "Hello!", "target": "speaker"}'

## Telegram voice message
curl -X POST http://butler:8888/tts/speak \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"text": "Hello!", "target": "telegram"}'
# Then respond with: MEDIA:/tmp/trulla_voice.ogg
```

---

## Adding New Service Endpoints

Want your AI to control a new service? Two steps (no code changes!):

### 1. Add to butler.yaml

```yaml
services:
  my-service:
    url: "http://10.0.0.5:3000"
    auth: bearer
    vault_key: my-service-token
    description: "What this service does"
```

### 2. Store the credential

```bash
# Option A: Flat file
echo "my-api-token" > /data/api/my-service

# Option B: Vaultwarden
# Create item named "my-service-token" with the API key in Notes
```

Then reload: `POST /config/reload`

The AI discovers the new service automatically via `GET /`. No SOUL.md update, no container rebuild.

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
`qwen3-coder:480b` would write a Python script to call the API instead of actually calling it. Then it would say "Done!" without executing anything. **Switch to a general-purpose model.**

### 2. Sub-Agent Delegation Doesn't Work (Yet)
We tried having the main AI delegate tasks to sub-agents. The sub-agents hallucinated even worse. **Disabled delegation entirely** – the main AI does everything itself.

### 3. FastAPI Catch-All Routes Are Greedy
`/{service}/{path:path}` matches EVERYTHING, including your specific routes like `/vm/list`. **Fix:** add a `SKIP_SERVICES` set at the top of the catch-all handler.

### 4. Docker Containers Don't Have SSH
When Butler needs to SSH to other hosts for VM creation, the `python:slim` image doesn't include `openssh-client`. **Add it to the Dockerfile.**

### 5. Docker Volumes Die with `down -v`
Named volumes (`database:`) get deleted by `docker compose down -v`. **Use bind mounts** (`/app-config/db:/var/lib/postgresql/data`) for anything you want to keep and backup.

### 6. Vaultwarden MCP + dotenv v17 = Broken
`dotenv` v17 writes to stdout on load, which breaks MCP's JSON-RPC protocol. **Pin to v16:** `npm install dotenv@16`.

### 7. The AI Will Break Your Network
Our AI once created a VM with the same IP as the host it was running on. **Add explicit rules to SOUL.md** about what NOT to do. Use `?dry_run=true` on destructive operations.

### 8. Keep SOUL.md Small
The AI's system prompt has limited space. Don't dump your entire infrastructure docs in there. With Butler v2.1, the AI can self-discover services via `GET /` – **SOUL.md only needs rules and personality.**

### 9. External Config > Hardcoded
We started with a hardcoded `SERVICES` dict in Python. Every service change required a container rebuild. Moving to `butler.yaml` + `POST /config/reload` was a game-changer – **add services in seconds.**

### 10. Audit Everything
Without an audit log, debugging "what did the AI do?" means digging through chat session files. **Butler's `/audit` endpoint** logs every call with timestamp, endpoint, status, and dry-run flag.

### 11. Outline API Authentication
Outline uses Bearer token auth via API keys created in the admin panel. Store the token in Vaultwarden as `outline-api-key` and reference it in butler.yaml.

### 12. Proxmox Node Numbering
VM subnet follows node number: `10.X.1.Y` where X is the node number. Node 5 → `10.5.1.Y`. This convention prevents IP conflicts across the cluster.

---

## Replication Checklist

To replicate this setup in your homelab:

### Phase 1: Infrastructure
- [ ] Proxmox VE cluster (1+ nodes)
- [ ] Dedicated automation host (VM or physical)
- [ ] Vaultwarden instance
- [ ] WireGuard VPN (optional, for remote access)

### Phase 2: Butler API
- [ ] Clone this repo
- [ ] Configure `butler.yaml` with your services
- [ ] Add credentials (flat files or Vaultwarden)
- [ ] Deploy with Docker Compose
- [ ] Test `GET /` and `GET /health`

### Phase 3: AI Agent
- [ ] Install Hermes
- [ ] Configure LLM provider (Ollama Cloud recommended)
- [ ] Create SOUL.md with rules
- [ ] Add skills for common tasks
- [ ] Connect to Butler API

### Phase 4: Automation
- [ ] Set up iso-builder scripts
- [ ] Create Ansible playbooks
- [ ] Configure SSH keys between Butler and automation host
- [ ] Test VM creation end-to-end

### Phase 5: Integration
- [ ] Set up Telegram bot for chat interface
- [ ] Configure TTS for voice feedback
- [ ] Enable audit logging
- [ ] Create monitoring dashboards

---

## License

MIT – take it, adapt it, make your homelab smarter.

Built by [@feldjaeger](https://github.com/feldjaeger) with Kiro 🤖 and Trulla 🍳.
