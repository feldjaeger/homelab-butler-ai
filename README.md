# 🤵 Homelab Butler

**One API to manage your entire homelab.** Designed as the single entry point for AI agents to control infrastructure via simple HTTP calls.

Born from the real-world need of getting an AI assistant ([Hermes/Trulla](https://github.com/lks-ai/hermes)) to reliably manage a 7-node Proxmox homelab. After weeks of the AI hallucinating success instead of actually making API calls, we built Butler to make every operation a single, simple HTTP request.

## What it does

| Category | Endpoints | What happens |
|----------|-----------|-------------|
| **Service Proxy** | `/{service}/{path}` | Routes to 15+ backend services with automatic auth injection |
| **VM Lifecycle** | `POST /vm/create` | One call → ISO build, VM create, SSH wait, Ansible setup (~10 min) |
| **Inventory** | `POST /inventory/host` | Adds host to Ansible inventory with group structure |
| **TTS** | `POST /tts/speak` | Text-to-speech on physical speaker or as Telegram voice message |

## The Problem We Solved

AI agents (LLMs with tool-calling) are great at making HTTP requests. They're terrible at:
- Multi-step SSH chains across hosts
- Remembering which credentials go where
- Waiting for async operations (VM boot, Ansible runs)
- Not hallucinating that they did something when they didn't

**Butler wraps all that complexity into single API calls.** The AI just needs to `curl -X POST /vm/create` and gets back a fully provisioned VM with Docker, backups, and monitoring.

### LLM Selection Matters

We tested multiple models for agent reliability:

| Model | Tool-Calling | Result |
|-------|-------------|--------|
| `qwen3-coder:480b` | ❌ Hallucinated success | Said "done!" without making any API calls |
| `qwen3.5:397b` | ✅ Actually works | Makes real HTTP calls, follows instructions |
| `minimax-m2.7` | ✅ Works well | 97% skill adherence per benchmarks |

**Key insight:** Coding models ≠ agent models. Use general-purpose models with native tool-calling for infrastructure automation.

## Quick Start

```bash
git clone https://github.com/feldjaeger/homelab-butler.git
cd homelab-butler
cp .env.example .env
# Edit .env with your values

# Add API keys as flat files
mkdir -p api-keys
echo "your-sonarr-key" > api-keys/sonarr
echo "your-radarr-key" > api-keys/radarr

# Edit SERVICES dict in app.py with your backends

docker compose up -d
curl http://localhost:8888/health
```

## Architecture

```
AI Agent (Hermes/Trulla/ChatGPT/etc.)
    │
    ▼
Butler API (:8888)  ─── single auth token ───
    │
    ├── Service Proxy ──→ Sonarr, Radarr, Grafana, n8n, ...
    │                     (auto-injects API keys per service)
    │
    ├── VM Lifecycle ──→ Proxmox API
    │       │
    │       └──→ automation host via SSH
    │              ├── iso-builder (custom Debian ISOs)
    │              └── Ansible (base setup)
    │
    └── TTS ──→ Chatterbox (GPU) ──→ Pi Speaker / Telegram
```

## Configuration

### Adding a Service

Edit the `SERVICES` dict in `app.py`:

```python
SERVICES = {
    "sonarr": {
        "url": "http://10.0.0.1:8989",
        "auth": "apikey",           # apikey | bearer | session | proxmox | n8n
        "vault_key": "sonarr-key",  # name in vault cache
        "key_file": "sonarr",       # fallback flat file in /data/api/
    },
}
```

### Auth Types

| Type | Header | Use for |
|------|--------|---------|
| `apikey` | `X-Api-Key: <key>` | Sonarr, Radarr, Seerr, WAHA |
| `bearer` | `Authorization: Bearer <key>` | Outline, Grafana, Home Assistant |
| `n8n` | `X-N8N-API-KEY: <key>` | n8n |
| `proxmox` | `PVEAPIToken=<id>=<secret>` | Proxmox VE |
| `session` | Cookie-based login | Dockhand |

### Credentials

Butler reads credentials in order:
1. **Vaultwarden cache** (synced by host cron to `/data/vault-cache/`)
2. **Flat files** in `/data/api/<name>`

This means you can start with flat files and migrate to Vaultwarden later.

## VM Lifecycle (the killer feature)

```bash
# Create a complete VM with one call
curl -X POST http://butler:8888/vm/create \
  -H "Authorization: Bearer $TOKEN" \
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
1. Builds a custom Debian ISO with preseed (static IP, SSH keys, user)
2. Uploads ISO to the Proxmox node
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
  "steps": ["iso-builder: ok", "ssh: my-service reachable", "inventory: added", "ansible: ok"]
}
```

## For AI Agents

### Prompt Template

Add this to your AI agent's system prompt or skill file:

```markdown
## Infrastructure Management via Butler API

Base URL: http://BUTLER_IP:8888
Auth: Authorization: Bearer YOUR_TOKEN

### Quick Reference
- List VMs: GET /vm/list
- Create VM: POST /vm/create {"node": N, "ip": "IP", "hostname": "NAME", "cores": 2, "memory": 4096, "disk": 32}
- Delete VM: DELETE /vm/{vmid}
- Proxy to any service: GET/POST /{service}/{api_path}
- TTS on speaker: POST /tts/speak {"text": "Hello!", "target": "speaker"}

### Rules
- ALWAYS use Butler API, never SSH directly to services
- VM creation takes ~10 minutes, set timeout to 700s
- All Docker volumes under /app-config/
- Git (Forgejo) is source of truth for compose files
```

### Why This Works for AI

1. **Single auth** – one Bearer token for everything
2. **Flat API** – no nested auth flows, no session management
3. **Synchronous** – `/vm/create` blocks until done (with progress in `steps`)
4. **Idempotent** – `/inventory/host` won't duplicate entries
5. **Self-documenting** – `GET /` returns all available services

## Requirements

- **Proxmox VE** cluster (for VM lifecycle)
- **Docker** on the Butler host
- **SSH access** from Butler container to automation host
- **iso-builder** script (for custom Debian ISOs) – or remove VM endpoints
- **Ansible** playbooks for base setup – or customize the setup step

## Adapting for Your Homelab

1. Fork this repo
2. Edit `SERVICES` dict with your backends and IPs
3. Add your API keys as flat files or set up Vaultwarden sync
4. Remove features you don't need (VM lifecycle, TTS, etc.)
5. Point your AI agent at `http://butler:8888`

The code is intentionally simple (~300 lines of Python). Read it, understand it, make it yours.

## License

MIT – do whatever you want with it.

## Credits

Built by [@feldjaeger](https://github.com/feldjaeger) with help from Kiro 🤖 and Trulla 🍳 (who finally learned to make actual API calls instead of hallucinating them).
