"""Homelab Butler v2.1 – Unified API proxy for Pfannkuchen homelab.
Reads service config from butler.yaml, credentials from Vaultwarden cache with flat-file fallback."""

import os, json, asyncio, logging, time
from datetime import datetime, timezone
import httpx, yaml
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from contextlib import asynccontextmanager

log = logging.getLogger("butler")

API_DIR = os.environ.get("API_KEY_DIR", "/data/api")
VAULT_CACHE_DIR = os.environ.get("VAULT_CACHE_DIR", "/data/vault-cache")
BUTLER_TOKEN = os.environ.get("BUTLER_TOKEN", "")
CONFIG_PATH = os.environ.get("BUTLER_CONFIG", "/data/butler.yaml")

# --- Config loading ---

_config: dict = {}

def _load_config():
    global _config, SERVICES, VM_CFG, TTS_CFG
    try:
        with open(CONFIG_PATH) as f:
            _config = yaml.safe_load(f) or {}
        SERVICES = _config.get("services", {})
        VM_CFG = _config.get("vm", {})
        TTS_CFG = _config.get("tts", {})
        log.info(f"Loaded config: {len(SERVICES)} services")
    except FileNotFoundError:
        log.warning(f"No config at {CONFIG_PATH}, using defaults")
        SERVICES = {}
        VM_CFG = {}
        TTS_CFG = {}

SERVICES: dict = {}
VM_CFG: dict = {}
TTS_CFG: dict = {}

# --- Audit log ---

_audit_log: list[dict] = []
MAX_AUDIT = 500

def _audit(endpoint: str, method: str, status: int, detail: str = "", dry_run: bool = False):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "method": method,
        "status": status,
        "detail": detail[:200],
        "dry_run": dry_run,
    }
    _audit_log.append(entry)
    if len(_audit_log) > MAX_AUDIT:
        _audit_log.pop(0)

API_DIR = os.environ.get("API_KEY_DIR", "/data/api")
VAULT_CACHE_DIR = os.environ.get("VAULT_CACHE_DIR", "/data/vault-cache")
BUTLER_TOKEN = os.environ.get("BUTLER_TOKEN", "")

# --- Credential cache ---

_vault_cache: dict[str, str] = {}

def _load_vault_cache():
    """Load vault items from disk cache (written by host-side vault-sync.sh)."""
    global _vault_cache
    if not os.path.isdir(VAULT_CACHE_DIR):
        log.info(f"No vault cache at {VAULT_CACHE_DIR}")
        return
    new = {}
    for f in os.listdir(VAULT_CACHE_DIR):
        path = os.path.join(VAULT_CACHE_DIR, f)
        if os.path.isfile(path):
            new[f] = open(path).read().strip()
    _vault_cache = new
    log.info(f"Loaded {len(new)} vault items from cache")

async def _periodic_cache_reload():
    """Reload vault cache every 5 minutes (host cron writes new files)."""
    while True:
        await asyncio.sleep(300)
        _load_vault_cache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_config()
    _load_vault_cache()
    task = asyncio.create_task(_periodic_cache_reload())
    yield
    task.cancel()

app = FastAPI(title="Homelab Butler", version="2.1.0", lifespan=lifespan,
              description="Unified API proxy + infrastructure management. AI agents: see GET / for self-onboarding.")

# --- Credential reading (vault-first, file-fallback) ---

def _read(name):
    """Read credential: vault cache first, then flat file."""
    # Vault cache uses lowercase-hyphenated names
    vault_name = name.lower().replace("_", "-")
    if vault_name in _vault_cache:
        return _vault_cache[vault_name]
    # Try uppercase convention
    upper = name.upper().replace("-", "_").lower().replace("_", "-")
    if upper in _vault_cache:
        return _vault_cache[upper]
    # Fallback to flat file
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

def _parse_url_key(name):
    raw = _read(name)
    if not raw:
        return None, None
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return (lines[0] if lines else None, lines[1] if len(lines) > 1 else None)

# --- Service configs loaded from butler.yaml ---

# --- Dockhand session ---

_dockhand_cookie = None

async def _dockhand_login(client):
    global _dockhand_cookie
    r = await client.post(
        f"{SERVICES['dockhand']['url']}/api/auth/login",
        json={"username": "admin", "password": _read("dockhand") or ""},
    )
    if r.status_code == 200:
        _dockhand_cookie = dict(r.cookies)
    return _dockhand_cookie

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

# --- Routes ---

@app.get("/")
async def root():
    """AI self-onboarding: returns all available endpoints and services."""
    svc_list = {}
    for name, cfg in SERVICES.items():
        svc_list[name] = {"url": cfg.get("url", ""), "auth": cfg.get("auth", ""), "description": cfg.get("description", "")}
    return {
        "service": "homelab-butler", "version": "2.1.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "services": svc_list,
        "endpoints": {
            "proxy": "GET/POST/PUT/DELETE /{service}/{path} - proxy to backend with auto-auth",
            "vm_list": "GET /vm/list",
            "vm_create": "POST /vm/create {node, ip, hostname, cores?, memory?, disk?}",
            "vm_status": "GET /vm/status/{vmid}",
            "vm_delete": "DELETE /vm/{vmid}",
            "inventory_add": "POST /inventory/host {name, ip, group?}",
            "ansible_run": "POST /ansible/run {hostname}",
            "tts_speak": "POST /tts/speak {text, target: speaker|telegram}",
            "tts_voices": "GET /tts/voices",
            "tts_health": "GET /tts/health",
            "status": "GET /status - health of all backends",
            "audit": "GET /audit - recent API calls",
        },
        "vault_items": len(_vault_cache),
    }

@app.get("/health")
async def health():
    return {"status": "ok", "vault_items": len(_vault_cache), "services": len(SERVICES), "version": "2.1.0"}

@app.get("/status")
async def status(_=Depends(_verify)):
    """Health check all configured backend services."""
    results = {}
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        for name, cfg in SERVICES.items():
            url = cfg.get("url")
            if not url:
                results[name] = {"status": "no_url"}
                continue
            try:
                r = await c.get(url, follow_redirects=True)
                results[name] = {"status": "ok", "http": r.status_code}
            except Exception as e:
                results[name] = {"status": "offline", "error": type(e).__name__}
    _audit("/status", "GET", 200)
    return results

@app.get("/audit")
async def audit(_=Depends(_verify), limit: int = Query(50, le=MAX_AUDIT)):
    """Recent API calls (newest first)."""
    return list(reversed(_audit_log[-limit:]))

@app.post("/config/reload")
async def config_reload(_=Depends(_verify)):
    """Reload butler.yaml and vault cache."""
    _load_config()
    _load_vault_cache()
    return {"config_services": len(SERVICES), "vault_items": len(_vault_cache)}

@app.post("/vault/reload")
async def vault_reload(_=Depends(_verify)):
    _load_vault_cache()
    return {"reloaded": True, "items": len(_vault_cache)}


# --- VM Lifecycle Endpoints ---
from pydantic import BaseModel
import subprocess as _sp

AUTOMATION1 = VM_CFG.get("automation_host", "user@10.0.0.2") if VM_CFG else "user@10.0.0.2"
ISO_BUILDER = VM_CFG.get("iso_builder_path", "/app-config/ansible/iso-builder/build-iso.sh") if VM_CFG else "/app-config/ansible/iso-builder/build-iso.sh"

class VMCreate(BaseModel):
    node: int
    ip: str
    hostname: str
    cores: int = 2
    memory: int = 4096
    disk: int = 32

def _ssh(host, cmd, timeout=600):
    r = _sp.run(["ssh","-o","ConnectTimeout=10","-o","StrictHostKeyChecking=accept-new",host,cmd],
                capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def _pve_auth():
    pv = _parse_kv("proxmox")
    return f"PVEAPIToken={pv.get('tokenid','')}={pv.get('secret','')}"

@app.get("/vm/list")
async def vm_list(_=Depends(_verify)):
    auth = _pve_auth()
    vms = []
    async with httpx.AsyncClient(verify=False, timeout=15) as c:
        nodes = await c.get("https://10.0.0.2:8006/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.get(f"https://10.0.0.2:8006/api2/json/nodes/{n['node']}/qemu", headers={"Authorization": auth})
            for vm in r.json().get("data", []):
                vm["node"] = n["node"]
                vms.append(vm)
    return vms

@app.post("/vm/create")
async def vm_create(req: VMCreate, _=Depends(_verify), dry_run: bool = Query(False)):
    if dry_run:
        _audit("/vm/create", "POST", 200, f"dry_run: {req.hostname} {req.ip} node{req.node}", dry_run=True)
        return {"dry_run": True, "would_create": {"hostname": req.hostname, "ip": req.ip, "node": req.node,
                "cores": req.cores, "memory": req.memory, "disk": req.disk},
                "steps": ["iso-builder", "wait ssh", "add inventory", "ansible setup"]}
    steps = []
    # Step 1: Build ISO + create VM via iso-builder on automation1
    cmd = f"{ISO_BUILDER} --node {req.node} --ip {req.ip} --hostname {req.hostname} --cores {req.cores} --memory {req.memory} --disk {req.disk} --password '{VM_CFG.get('default_password', 'changeme')}' --create-vm"
    rc, out, err = _ssh(AUTOMATION1, f"cd /app-config/ansible/iso-builder && {cmd}", timeout=300)
    if rc != 0:
        return JSONResponse({"error": "iso-builder failed", "stderr": err[-500:], "stdout": out[-500:]}, status_code=500)
    steps.append("iso-builder: ok")

    # Step 2: Wait for SSH (up to 6 min)
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

    # Step 2.5: Add to Ansible inventory
    ini = "/app-config/ansible/pfannkuchen.ini"
    group = getattr(req, 'group', 'auto')
    inv_cmd = f"""python3 -c "
lines = open('{ini}').readlines()
if not any('{req.hostname} ' in l for l in lines):
    out = []
    found = False
    for l in lines:
        out.append(l)
        if l.strip() == '[auto]':
            found = True
        elif found and (l.startswith('[') or l.strip() == ''):
            out.insert(-1, '{req.hostname} ansible_host={req.ip}\\n')
            found = False
    if found:
        out.append('{req.hostname} ansible_host={req.ip}\\n')
    open('{ini}','w').writelines(out)
    print('added')
else:
    print('exists')
" """
    _ssh(AUTOMATION1, inv_cmd, timeout=30)
    _ssh(AUTOMATION1, f"mkdir -p /app-config/ansible/host_vars/{req.hostname} && printf 'ansible_host: {req.ip}\\nansible_user: user\\n' > /app-config/ansible/host_vars/{req.hostname}/vars.yml", timeout=30)
    _ssh(AUTOMATION1, f"ssh-keygen -f /home/user/.ssh/known_hosts -R {req.ip} 2>/dev/null; ssh -o StrictHostKeyChecking=accept-new user@{req.ip} hostname 2>/dev/null", timeout=30)
    steps.append("inventory: added")

    # Step 3: Ansible base setup via direct SSH (reliable fallback)
    rc3, _, err3 = _ssh(AUTOMATION1, f"cd /app-config/ansible && bash pfannkuchen.sh setup {req.hostname}", timeout=600)
    steps.append(f"ansible: {'ok' if rc3 == 0 else 'failed (rc=' + str(rc3) + ')'}")

    _audit("/vm/create", "POST", 200 if rc3 == 0 else 500, f"{req.hostname} {req.ip}")
    return {"status": "ok" if rc3 == 0 else "partial", "hostname": req.hostname, "ip": req.ip, "node": req.node, "steps": steps}

@app.get("/vm/status/{vmid}")
async def vm_status(vmid: int, _=Depends(_verify)):
    auth = _pve_auth()
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        nodes = await c.get("https://10.0.0.2:8006/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.get(f"https://10.0.0.2:8006/api2/json/nodes/{n['node']}/qemu/{vmid}/status/current", headers={"Authorization": auth})
            if r.status_code == 200:
                return r.json().get("data", {})
    return JSONResponse({"error": "VM not found"}, status_code=404)

@app.delete("/vm/{vmid}")
async def vm_delete(vmid: int, _=Depends(_verify)):
    auth = _pve_auth()
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        nodes = await c.get("https://10.0.0.2:8006/api2/json/nodes", headers={"Authorization": auth})
        for n in nodes.json().get("data", []):
            r = await c.delete(f"https://10.0.0.2:8006/api2/json/nodes/{n['node']}/qemu/{vmid}", headers={"Authorization": auth})
            if r.status_code == 200:
                return r.json()
    return JSONResponse({"error": "VM not found"}, status_code=404)

@app.post("/inventory/host")
async def inventory_host(request: Request, _=Depends(_verify)):
    body = await request.json()
    name, ip = body["name"], body["ip"]
    group = body.get("group", "auto")
    user = body.get("user", "user")
    ini = "/app-config/ansible/pfannkuchen.ini"
    # Add host to group in pfannkuchen.ini (idempotent)
    add_cmd = f"""python3 -c "
lines = open('{ini}').readlines()
# Check if host already exists
if any('{name} ' in l or '{name}\\n' in l for l in lines):
    print('already exists')
else:
    # Find the group and insert after it
    out, found = [], False
    for l in lines:
        out.append(l)
        if l.strip() == '[{group}]':
            found = True
        elif found and (l.startswith('[') or l.strip() == ''):
            out.insert(-1, '{name} ansible_host={ip}\\n')
            found = False
    if found:  # group was last
        out.append('{name} ansible_host={ip}\\n')
    open('{ini}','w').writelines(out)
    print('added to [{group}]')
" """
    rc, out, _ = _ssh(AUTOMATION1, add_cmd, timeout=30)
    # Also create host_vars
    _ssh(AUTOMATION1, f"mkdir -p /app-config/ansible/host_vars/{name} && printf 'ansible_host: {ip}\\nansible_user: {user}\\n' > /app-config/ansible/host_vars/{name}/vars.yml", timeout=30)
    return {"status": "ok", "name": name, "ip": ip, "group": group, "result": out.strip()}

@app.post("/ansible/run")
async def ansible_run(request: Request, _=Depends(_verify)):
    body = await request.json()
    hostname = body.get("limit", body.get("hostname", ""))
    template_id = body.get("template_id", 10)
    if not hostname:
        return JSONResponse({"error": "limit/hostname required"}, status_code=400)
    rc, out, err = _ssh(AUTOMATION1, f"cd /app-config/ansible && bash pfannkuchen.sh setup {hostname}", timeout=600)
    return {"status": "ok" if rc == 0 else "error", "rc": rc, "output": out[-1000:]}

@app.get("/ansible/status/{job_id}")
async def ansible_status(job_id: int, _=Depends(_verify)):
    return {"info": "direct SSH mode - no async job tracking"}

# --- TTS Endpoints ---

class TTSRequest(BaseModel):
    text: str
    target: str = "speaker"  # "speaker" or "telegram"
    voice: str = "deep_thought.mp3"
    language: str = "de"

SPEAKER_URL = TTS_CFG.get("speaker_url", "http://10.0.0.4:10800") if TTS_CFG else "http://10.0.0.4:10800"
CHATTERBOX_URL = TTS_CFG.get("chatterbox_url", "http://10.0.0.3:8004/tts") if TTS_CFG else "http://10.0.0.3:8004/tts"

@app.post("/tts/speak")
async def tts_speak(req: TTSRequest, _=Depends(_verify)):
    if req.target == "speaker":
        async with httpx.AsyncClient(verify=False, timeout=120) as c:
            r = await c.post(SPEAKER_URL, json={"text": req.text})
            return {"status": "ok" if r.status_code == 200 else "error", "target": "speaker"}
    elif req.target == "telegram":
        # Generate WAV via Chatterbox, save to hermes VM as OGG for Telegram voice
        async with httpx.AsyncClient(verify=False, timeout=120) as c:
            r = await c.post(CHATTERBOX_URL, json={
                "text": req.text, "voice_mode": "clone",
                "reference_audio_filename": req.voice,
                "output_format": "wav", "language": req.language,
                "exaggeration": 0.3, "cfg_weight": 0.7, "temperature": 0.6,
            })
            if r.status_code != 200:
                return JSONResponse({"error": "chatterbox failed"}, status_code=500)
        # Save WAV and convert to OGG on hermes
        import tempfile
        wav_path = tempfile.mktemp(suffix=".wav")
        ogg_path = "/tmp/trulla_voice.ogg"
        with open(wav_path, "wb") as f:
            f.write(r.content)
        rc, _, _ = _ssh("user@10.0.0.5", f"rm -f {ogg_path}", timeout=10)
        # Copy WAV to hermes and convert
        _sp.run(["scp", "-o", "ConnectTimeout=5", wav_path, f"user@10.0.0.5:/tmp/trulla_voice.wav"], timeout=30)
        _ssh("user@10.0.0.5", f"ffmpeg -y -i /tmp/trulla_voice.wav -c:a libopus -b:a 64k {ogg_path} 2>/dev/null", timeout=30)
        os.unlink(wav_path)
        return {"status": "ok", "target": "telegram", "media_path": ogg_path, "hint": "Use MEDIA:/tmp/trulla_voice.ogg in response"}
    else:
        return JSONResponse({"error": f"unknown target: {req.target}"}, status_code=400)

@app.get("/tts/voices")
async def tts_voices(_=Depends(_verify)):
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        r = await c.get("http://10.0.0.3:8004/get_predefined_voices")
        return r.json()

@app.get("/tts/health")
async def tts_health(_=Depends(_verify)):
    results = {}
    async with httpx.AsyncClient(verify=False, timeout=5) as c:
        try:
            r = await c.get(SPEAKER_URL)
            results["speaker"] = r.json()
        except Exception as e:
            results["speaker"] = {"status": "offline", "error": str(e)}
        try:
            r = await c.get("http://10.0.0.3:8004/api/model-info")
            results["chatterbox"] = "ok"
        except Exception as e:
            results["chatterbox"] = {"status": "offline", "error": str(e)}
    return results


@app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(service: str, path: str, request: Request, _=Depends(_verify)):
    SKIP_SERVICES = {"vm", "inventory", "ansible", "debug", "tts", "status", "audit", "config"}
    if service in SKIP_SERVICES:
        raise HTTPException(404, f"Unknown service: {service}")
    cfg = SERVICES.get(service)
    if not cfg:
        raise HTTPException(404, f"Unknown service: {service}. Available: {list(SERVICES.keys())}")

    base_url = cfg["url"]
    auth_type = cfg["auth"]
    headers = dict(request.headers)
    cookies = {}

    for h in ["host", "content-length", "transfer-encoding", "authorization"]:
        headers.pop(h, None)

    if auth_type == "apikey":
        headers["X-Api-Key"] = _get_key(cfg) or ""
    elif auth_type == "apikey_urlfile":
        url, key = _parse_url_key(cfg["key_file"])
        base_url = url.rstrip("/") if url else ""
        headers["X-Api-Key"] = key or ""
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {_get_key(cfg)}"
    elif auth_type == "n8n":
        headers["X-N8N-API-KEY"] = _get_key(cfg) or ""
    elif auth_type == "proxmox":
        pv = _parse_kv("proxmox")
        headers["Authorization"] = f"PVEAPIToken={pv.get('tokenid', '')}={pv.get('secret', '')}"
    elif auth_type == "session":
        global _dockhand_cookie
        if not _dockhand_cookie:
            async with httpx.AsyncClient(verify=False) as c:
                await _dockhand_login(c)
        cookies = _dockhand_cookie or {}

    target = f"{base_url}/{path}"
    body = await request.body()

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.request(method=request.method, url=target,
                                    headers=headers, cookies=cookies, content=body)
        if auth_type == "session" and resp.status_code == 401:
            _dockhand_cookie = None
            await _dockhand_login(client)
            resp = await client.request(method=request.method, url=target,
                                        headers=headers, cookies=_dockhand_cookie or {}, content=body)

    try:
        data = resp.json()
    except Exception:
        data = resp.text
    _audit(f"/{service}/{path}", request.method, resp.status_code)
    return JSONResponse(content=data, status_code=resp.status_code)



