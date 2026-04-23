# 🤵 Homelab Butler API

**One API to manage your entire homelab.** Designed as the single entry point for AI agents to control infrastructure via simple HTTP calls.

> **Version:** 2.1.0  
> **Base URL:** `http://BUTLER_IP:8888`  
> **Auth:** `Authorization: Bearer YOUR_TOKEN`

---

## Quick Reference for AI Agents

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | **Self-discovery** – returns all services and endpoints |
| `/health` | GET | Health check |
| `/status` | GET | Health of all backend services |
| `/audit` | GET | Recent API calls (debugging) |
| `/config/reload` | POST | Reload butler.yaml and vault cache |
| `/vault/reload` | POST | Force vault cache reload |

### VM Lifecycle

| Endpoint | Method | Body / Params |
|----------|--------|---------------|
| `/vm/list` | GET | – |
| `/vm/create` | POST | `{"node": 5, "ip": "10.5.1.115", "hostname": "my-vm", "cores": 2, "memory": 4096, "disk": 32}` |
| `/vm/status/{vmid}` | GET | – |
| `/vm/{vmid}` | DELETE | – |

**VM Creation Flow (automatic):**
1. Builds custom Debian ISO with preseed (static IP, SSH keys, user)
2. Uploads ISO to Proxmox node
3. Creates VM with EFI boot, SCSI disk, virtio NIC
4. Starts VM and waits for SSH (~5 min)
5. Adds host to Ansible inventory
6. Runs Ansible base setup (Docker, Borgmatic, monitoring)

**Timeout:** Set to **700s** – VM creation takes ~10 minutes!

### Service Proxy

Access any backend service through Butler with automatic auth injection:

```
GET/POST/PUT/DELETE /{service}/{path}
```

| Service | Auth Type | Example Path |
|---------|-----------|--------------|
| `sonarr` | apikey | `/sonarr/api/v3/series` |
| `radarr` | apikey | `/radarr/api/v3/movie` |
| `grafana` | bearer | `/grafana/api/dashboards` |
| `homeassistant` | bearer | `/homeassistant/api/states` |
| `proxmox` | proxmox | `/proxmox/api2/json/nodes` |
| `n8n` | n8n | `/n8n/api/v1/workflows` |
| `outline` | bearer | `/outline/api/documents.search` |
| `forgejo` | bearer | `/forgejo/api/v1/user/repos` |
| `dockhand` | session | `/dockhand/api/stacks` |
| `uptime` | bearer | `/uptime/api/status-page` |
| `waha` | apikey | `/waha/api/sendText` |
| `seerr` | apikey | `/seerr/api/v1/requests` |
| `tdarr` | apikey | `/tdarr/api/v2/collection` |
| `immich` | apikey | `/immich/api/assets` |
| `sabnzbd` | apikey | `/sabnzbd/api?mode=queue` |

### TTS (Text-to-Speech)

| Endpoint | Method | Body |
|----------|--------|------|
| `/tts/speak` | POST | `{"text": "Hello!", "target": "speaker\|telegram", "voice": "deep_thought.mp3"}` |
| `/tts/voices` | GET | – |
| `/tts/health` | GET | – |

### Ansible Integration

| Endpoint | Method | Body |
|----------|--------|------|
| `/inventory/host` | POST | `{"name": "hostname", "ip": "10.X.1.Y", "group": "optional"}` |
| `/ansible/run` | POST | `{"hostname": "my-vm"}` |
| `/ansible/status/{job_id}` | GET | – |

---

## Configuration

### Adding a Service

Edit `butler.yaml`:

```yaml
services:
  my-service:
    url: "http://10.X.1.Y:PORT"
    auth: apikey  # or: bearer, n8n, proxmox, session
    vault_key: my-service-token
    description: "What this service does"
```

Then reload: `POST /config/reload`

### Auth Types

| Type | Header | Used By |
|------|--------|---------|
| `apikey` | `X-Api-Key: <key>` | Sonarr, Radarr, Seerr, WAHA, Immich, SABnzbd, Tdarr |
| `bearer` | `Authorization: Bearer <token>` | Outline, Grafana, Home Assistant, Uptime Kuma, Forgejo |
| `n8n` | `X-N8N-API-KEY: <key>` | n8n |
| `proxmox` | `PVEAPIToken=<user>=<secret>` | Proxmox VE |
| `session` | Cookie-based | Dockhand |

### Credential Storage

Butler reads credentials in order:
1. **Vaultwarden cache** (`/data/vault-cache/` – synced by host cron)
2. **Flat files** (`/data/api/<name>`)

**Recommendation:** Start with flat files, migrate to Vaultwarden later.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  AI Agent (Hermes/Trulla)                               │
│  - LLM: qwen3.5:397b-cloud                              │
│  - Single Bearer Token for everything                   │
└──────────────┬──────────────────────────────────────────┘
               │ HTTP
┌──────────────▼──────────────────────────────────────────┐
│  Butler API (:8888)                                     │
│  ┌─────────────┬──────────────┬───────────┬──────────┐ │
│  │ Service     │ VM Lifecycle │ Ansible   │ TTS      │ │
│  │ Proxy       │ (Proxmox)   │ Inventory │ Speaker  │ │
│  │ 15 backends │ create/list  │ add host  │ Telegram │ │
│  │ auto-auth   │ delete/status│ run play  │          │ │
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

## Installation

### Prerequisites

- Docker + Docker Compose
- Proxmox VE cluster (for VM lifecycle)
- SSH access from Butler to automation host
- Vaultwarden (optional, for credential management)

### Quick Start

```bash
git clone https://github.com/feldjaeger/homelab-butler-ai.git
cd homelab-butler-ai

cp .env.example .env
cp butler.yaml.example butler.yaml

# Edit .env and butler.yaml with your values

# Add API keys as flat files
mkdir -p api-keys
echo "your-sonarr-key" > api-keys/sonarr
echo "your-radarr-key" > api-keys/radarr

docker compose up -d
curl http://localhost:8888/health
```

### Environment Variables (.env)

```bash
BUTLER_TOKEN=your-secret-token-here
PROXMOX_URL=https://proxmox-ip:8006
AUTOMATION_HOST=user@automation-ip
ISO_BUILDER_PATH=/opt/iso-builder/build-iso.sh
VM_DEFAULT_PASSWORD=changeme
SPEAKER_URL=http://pi-ip:10800
CHATTERBOX_URL=http://gpu-host:8004/tts
```

### Butler Config (butler.yaml)

```yaml
services:
  sonarr:
    url: "http://10.X.1.Y:8989"
    auth: apikey
    vault_key: sonarr-key
    description: "TV show management"

  radarr:
    url: "http://10.X.1.Y:7878"
    auth: apikey
    vault_key: radarr-key
    description: "Movie management"

vm:
  automation_host: "user@automation-ip"
  iso_builder_path: "/opt/iso-builder/build-iso.sh"
  proxmox_url: "https://proxmox-ip:8006"
  default_password: "changeme"
  ansible_base_path: "/opt/ansible"
  inventory_file: "/opt/ansible/inventory.ini"

tts:
  speaker_url: "http://pi-ip:10800"
  chatterbox_url: "http://gpu-host:8004/tts"
  default_voice: "deep_thought.mp3"
```

---

## For AI Agents

### System Prompt Template

```markdown
## Infrastructure Management via Butler API

Base URL: http://BUTLER_IP:8888
Auth: `Authorization: Bearer YOUR_TOKEN`

### Self-Discovery
- Call `GET /` to discover all available services and endpoints
- Call `GET /docs` for interactive Swagger UI
- Call `GET /status` to check backend health

### Quick Reference
- List VMs: `GET /vm/list`
- Create VM: `POST /vm/create` with `{"node": N, "ip": "10.X.1.Y", "hostname": "NAME", "cores": 2, "memory": 4096, "disk": 32}`
- Delete VM: `DELETE /vm/{vmid}`
- Proxy to any service: `GET/POST /{service}/{api_path}`
- TTS: `POST /tts/speak` with `{"text": "Hello!", "target": "speaker|telegram"}`

### Rules
- ALWAYS use Butler API, NEVER SSH directly to services
- ALWAYS use VMs, NEVER LXC containers
- VM creation takes ~10 minutes – set timeout to 700s
- All Docker volumes under /app-config/
- Git (Forgejo) is source of truth for compose files
- Use `?dry_run=true` before destructive operations if available
```

### Why This Works for AI

1. **Single auth** – one Bearer token for everything
2. **Flat API** – no nested auth flows, no session management
3. **Synchronous** – `/vm/create` blocks until done (with progress in `steps`)
4. **Idempotent** – `/inventory/host` won't duplicate entries
5. **Self-documenting** – `GET /` returns all available services
6. **Audit trail** – `GET /audit` shows what the AI did

---

## Troubleshooting

### Invalid Token
```json
{"detail": "Invalid token"}
```
→ Check `BUTLER_TOKEN` in `.env` and match with your Authorization header

### Service Not Found
```json
{"detail": "Service not configured"}
```
→ Add service to `butler.yaml` and `POST /config/reload`

### VM Creation Fails
1. Check Proxmox API access: `curl -k https://proxmox:8006/api2/json/nodes`
2. Verify SSH from Butler to automation host works
3. Check iso-builder script exists and is executable
4. Review audit log: `GET /audit?limit=50`

### Credential Issues
- Butler reads from `/data/vault-cache/` first, then `/data/api/`
- Ensure files contain only the raw key (no trailing newlines)
- Reload vault: `POST /vault/reload`

---

## Security Notes

⚠️ **NEVER commit credentials to Git!**

- Use `.env.example` with placeholder values
- Add `.env` and `api-keys/` to `.gitignore`
- Store real tokens in Vaultwarden or secure environment variables
- Butler token should be rotated periodically

---

## License

MIT – do whatever you want with it.

Built by [@feldjaeger](https://github.com/feldjaeger) with Kiro 🤖 and Trulla 🍳.
