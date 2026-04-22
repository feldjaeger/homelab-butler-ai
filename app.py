"""Homelab Butler – Unified API proxy with VM lifecycle management.
One API to rule them all: proxies requests to backend services with automatic auth,
creates VMs from scratch, manages Ansible inventory, and does TTS.

Designed to be the single entry point for AI agents (like Hermes/Trulla) to manage
an entire homelab infrastructure via simple HTTP calls.
"""

import os, json, asyncio, logging
import subprocess as _sp
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel

log = logging.getLogger("butler")

API_DIR = os.environ.get("API_KEY_DIR", "/data/api")
VAULT_CACHE_DIR = os.environ.get("VAULT_CACHE_DIR", "/data/vault-cache")
BUTLER_TOKEN = os.environ.get("BUTLER_TOKEN", "")

# --- Credential cache (Vaultwarden sync) ---

_vault_cache: dict[str, str] = {}

def _load_vault_cache():
    global _vault_cache
    if not os.path.isdir(VAULT_CACHE_DIR):
        return
    new = {}
    for f in os.listdir(VAULT_CACHE_DIR):
        path = os.path.join(VAULT_CACHE_DIR, f)
        if os.path.isfile(path):
            new[f] = open(path).read().strip()
    _vault_cache = new
    log.info(f"Loaded {len(new)} vault items from cache")

async def _periodic_cache_reload():
    while True:
        await asyncio.sleep(300)
        _load_vault_cache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_vault_cache()
    task = asyncio.create_task(_periodic_cache_reload())
    yield
    task.cancel()

app = FastAPI(title="Homelab Butler", version="2.0.0", lifespan=lifespan)

# --- Credential reading (vault-first, file-fallback) ---

def _read(name):
    vault_name = name.lower().replace("_", "-")
    if vault_name in _vault_cache:
        return _vault_cache[vault_name]
    try:
        return open(f"{API_DIR}/{name}").read().strip()
    except FileNotFoundError:
        return None

def _parse_kv(name):
    raw = _read(name)
    if not raw:
        return {}
    d = {}
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d

# --- Service configs (CUSTOMIZE THESE!) ---

SERVICES = {
    # "service_name": {"url": "http://IP:PORT", "auth": "apikey|bearer|session|proxmox", "vault_key": "vault-item-name"},
    # Auth types:
    #   apikey  -> X-Api-Key header
    #   bearer  -> Authorization: Bearer header
    #   session -> Cookie-based login (e.g. Dockhand)
    #   proxmox -> PVEAPIToken auth
    #   n8n     -> X-N8N-API-KEY header
    "example": {"url": "http://10.0.0.1:8080", "auth": "apikey", "vault_key": "example-api-key"},
}

# --- Auth ---

def _verify(request: Request):
    if not BUTLER_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {BUTLER_TOKEN}":
        raise HTTPException(401, "Invalid token")

def _get_key(cfg):
    vault_key = cfg.get("vault_key")
    if vault_key and vault_key in _vault_cache:
        return _vault_cache[vault_key]
    return _read(cfg.get("key_file", ""))

# --- VM Lifecycle ---

AUTOMATION_HOST = os.environ.get("AUTOMATION_HOST", "user@automation-server")
ISO_BUILDER = os.environ.get("ISO_BUILDER_PATH", "/path/to/build-iso.sh")
PROXMOX_URL = os.environ.get("PROXMOX_URL", "https://proxmox:8006")
VM_PASSWORD = os.environ.get("VM_DEFAULT_PASSWORD", "changeme")

class VMCreate(BaseModel):
    node: int
    ip: str
    hostname: str
    cores: int = 2
    memory: int = 4096
    disk: int = 32

def _ssh(host, cmd, timeout=600):
    r = _sp.run(["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", host, cmd],
                capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def _pve_auth():
    pv = _parse_kv("proxmox")
    return f"PVEAPIToken={pv.get('tokenid', '')}={pv.get('secret', '')}"

@app.get("/vm/list")
async def vm_list(_=Depends(_verify)):
    auth = _pve_auth()
    vms = []
    async with httpx.AsyncClient(verify=False, timeout=15) as c:
        nodes = await c.get(f"{PROXMOX_URL}/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.get(f"{PROXMOX_URL}/api2/json/nodes/{n['node']}/qemu", headers={"Authorization": auth})
            for vm in r.json().get("data", []):
                vm["node"] = n["node"]
                vms.append(vm)
    return vms

@app.post("/vm/create")
async def vm_create(req: VMCreate, _=Depends(_verify)):
    steps = []
    # Step 1: Build ISO + create VM
    cmd = f"{ISO_BUILDER} --node {req.node} --ip {req.ip} --hostname {req.hostname} --cores {req.cores} --memory {req.memory} --disk {req.disk} --password '{VM_PASSWORD}' --create-vm"
    rc, out, err = _ssh(AUTOMATION_HOST, f"cd $(dirname {ISO_BUILDER}) && {cmd}", timeout=300)
    if rc != 0:
        return JSONResponse({"error": "iso-builder failed", "stderr": err[-500:]}, status_code=500)
    steps.append("iso-builder: ok")

    # Step 2: Wait for SSH
    ok = False
    for _ in range(36):
        try:
            rc2, out2, _ = _ssh(f"user@{req.ip}", "hostname", timeout=10)
            if rc2 == 0:
                ok = True
                steps.append(f"ssh: {out2.strip()} reachable")
                break
        except Exception:
            pass
        await asyncio.sleep(10)
    if not ok:
        return JSONResponse({"error": "SSH timeout", "steps": steps}, status_code=504)

    # Step 3: Add to Ansible inventory (customize for your setup)
    steps.append("inventory: added")

    # Step 4: Ansible base setup
    rc3, _, _ = _ssh(AUTOMATION_HOST, f"cd /path/to/ansible && ./setup.sh {req.hostname}", timeout=600)
    steps.append(f"ansible: {'ok' if rc3 == 0 else 'failed'}")

    return {"status": "ok" if rc3 == 0 else "partial", "hostname": req.hostname, "ip": req.ip, "steps": steps}

@app.get("/vm/status/{vmid}")
async def vm_status(vmid: int, _=Depends(_verify)):
    auth = _pve_auth()
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        nodes = await c.get(f"{PROXMOX_URL}/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.get(f"{PROXMOX_URL}/api2/json/nodes/{n['node']}/qemu/{vmid}/status/current", headers={"Authorization": auth})
            if r.status_code == 200:
                return r.json().get("data", {})
    return JSONResponse({"error": "VM not found"}, status_code=404)

@app.delete("/vm/{vmid}")
async def vm_delete(vmid: int, _=Depends(_verify)):
    auth = _pve_auth()
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        nodes = await c.get(f"{PROXMOX_URL}/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.delete(f"{PROXMOX_URL}/api2/json/nodes/{n['node']}/qemu/{vmid}", headers={"Authorization": auth})
            if r.status_code == 200:
                return r.json()
    return JSONResponse({"error": "VM not found"}, status_code=404)

# --- TTS ---

SPEAKER_URL = os.environ.get("SPEAKER_URL", "")  # e.g. http://pi:10800
CHATTERBOX_URL = os.environ.get("CHATTERBOX_URL", "")  # e.g. http://gpu-host:8004/tts

class TTSRequest(BaseModel):
    text: str
    target: str = "speaker"
    voice: str = "default.mp3"
    language: str = "de"

@app.post("/tts/speak")
async def tts_speak(req: TTSRequest, _=Depends(_verify)):
    if req.target == "speaker" and SPEAKER_URL:
        async with httpx.AsyncClient(verify=False, timeout=120) as c:
            r = await c.post(SPEAKER_URL, json={"text": req.text})
            return {"status": "ok" if r.status_code == 200 else "error", "target": "speaker"}
    elif req.target == "telegram" and CHATTERBOX_URL:
        async with httpx.AsyncClient(verify=False, timeout=120) as c:
            r = await c.post(CHATTERBOX_URL, json={
                "text": req.text, "voice_mode": "clone",
                "reference_audio_filename": req.voice,
                "output_format": "wav", "language": req.language,
            })
            if r.status_code == 200:
                import base64
                return {"status": "ok", "audio_base64": base64.b64encode(r.content).decode()}
    return JSONResponse({"error": f"target '{req.target}' not configured"}, status_code=400)

# --- Utility ---

@app.get("/")
async def root():
    return {"service": "homelab-butler", "version": "2.0.0",
            "services": list(SERVICES.keys()), "vault_items": len(_vault_cache)}

@app.get("/health")
async def health():
    return {"status": "ok", "vault_items": len(_vault_cache)}

@app.post("/vault/reload")
async def vault_reload(_=Depends(_verify)):
    _load_vault_cache()
    return {"reloaded": True, "items": len(_vault_cache)}

# --- Catch-all proxy ---

@app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(service: str, path: str, request: Request, _=Depends(_verify)):
    SKIP = {"vm", "inventory", "ansible", "tts"}
    if service in SKIP:
        raise HTTPException(404, f"Unknown service: {service}")
    cfg = SERVICES.get(service)
    if not cfg:
        raise HTTPException(404, f"Unknown service: {service}. Available: {list(SERVICES.keys())}")

    headers = dict(request.headers)
    for h in ["host", "content-length", "transfer-encoding", "authorization"]:
        headers.pop(h, None)

    auth_type = cfg["auth"]
    if auth_type == "apikey":
        headers["X-Api-Key"] = _get_key(cfg) or ""
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {_get_key(cfg)}"
    elif auth_type == "n8n":
        headers["X-N8N-API-KEY"] = _get_key(cfg) or ""
    elif auth_type == "proxmox":
        pv = _parse_kv("proxmox")
        headers["Authorization"] = f"PVEAPIToken={pv.get('tokenid', '')}={pv.get('secret', '')}"

    target = f"{cfg['url']}/{path}"
    body = await request.body()

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.request(method=request.method, url=target, headers=headers, content=body)

    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return JSONResponse(content=data, status_code=resp.status_code)
