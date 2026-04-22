#!/bin/bash
# ============================================================
# Debian Unattended ISO Builder for Proxmox
# Builds a custom Debian ISO with embedded preseed config
# that auto-installs with static IP, SSH key, and user setup.
#
# Usage:
#   ./build-iso.sh --node 5 --ip 10.5.1.115 --hostname my-vm --create-vm
#
# Requirements: xorriso, cpio, gzip, genisoimage
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="/tmp/iso-builder-$$"
DEBIAN_VERSION="${DEBIAN_VERSION:-13}"
DEBIAN_CODENAME="${DEBIAN_CODENAME:-trixie}"
ARCH="amd64"

# --- Defaults (CUSTOMIZE THESE) ---
IP=""
NETMASK="255.255.255.0"
GATEWAY=""
DNS="1.1.1.1"
HOSTNAME="debian"
USER="user"
PASSWORD=""
SSH_KEY="ssh-ed25519 AAAA... your-key-here"  # <-- PUT YOUR SSH PUBLIC KEY HERE
OUTPUT_DIR="$SCRIPT_DIR/output"
NODE=""
VMID=""
CREATE_VM=false
VM_CORES=8
VM_MEMORY=16384
VM_DISK=64

# --- Node mapping (CUSTOMIZE FOR YOUR NETWORK) ---
# Maps node number to gateway IP and Proxmox host IP
declare -A NODE_GW=(
    [1]="10.1.1.1" [2]="10.2.1.1" [3]="10.3.1.1" [4]="10.4.1.1"
    [5]="10.5.1.1"  [6]="10.6.1.1" [7]="10.7.1.1"
)
declare -A NODE_IP=(
    [1]="10.0.0.11" [2]="10.0.0.12" [3]="10.0.0.13" [4]="10.0.0.14"
    [5]="10.0.0.15" [6]="10.0.0.16" [7]="10.0.0.17"
)

# --- Storage mapping (CUSTOMIZE FOR YOUR STORAGE) ---
# Which Proxmox storage to use per node
get_storage() {
    local node=$1
    # Example: some nodes use shared storage, others use local LVM
    echo "local-lvm"
}

usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Required:
  --node N             Proxmox node number (auto-sets gateway)
  --ip IP              Static IP address

Optional:
  --create-vm          Create VM on the node after ISO build
  --vmid ID            Specific VMID (auto-assigns if omitted)
  --cores N            CPU cores (default: 8)
  --memory MB          RAM in MB (default: 16384)
  --disk GB            Disk size in GB (default: 64)
  --hostname NAME      Hostname (default: debian)
  --user USER          Username (default: user)
  --password PASS      Password (prompted if not set)
  --dns DNS            DNS server (default: 1.1.1.1)

Examples:
  $0 --node 4 --ip 10.4.1.120 --hostname my-service
  $0 --node 5 --ip 10.5.1.115 --hostname my-vm --create-vm --cores 4 --memory 8192
EOF
  exit 1
}

# --- Parse Args ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --ip) IP="$2"; shift 2;;
    --netmask) NETMASK="$2"; shift 2;;
    --gateway) GATEWAY="$2"; shift 2;;
    --dns) DNS="$2"; shift 2;;
    --hostname) HOSTNAME="$2"; shift 2;;
    --user) USER="$2"; shift 2;;
    --password) PASSWORD="$2"; shift 2;;
    --output) OUTPUT_DIR="$2"; shift 2;;
    --debian-version) DEBIAN_VERSION="$2"; shift 2;;
    --node) NODE="$2"; shift 2;;
    --vmid) VMID="$2"; CREATE_VM=true; shift 2;;
    --create-vm) CREATE_VM=true; shift;;
    --cores) VM_CORES="$2"; shift 2;;
    --memory) VM_MEMORY="$2"; shift 2;;
    --disk) VM_DISK="$2"; shift 2;;
    *) echo "Unknown option: $1"; usage;;
  esac
done

# --- Resolve node ---
if [[ -n "$NODE" ]]; then
  [[ -z "${NODE_GW[$NODE]}" ]] && echo "Error: Invalid node $NODE" && exit 1
  GATEWAY="${NODE_GW[$NODE]}"
  PVE_HOST="${NODE_IP[$NODE]}"
fi

[[ -z "$IP" ]] && echo "Error: --ip is required" && usage
[[ -z "$GATEWAY" ]] && echo "Error: --gateway or --node is required" && usage

# --- Dependencies ---
for cmd in xorriso cpio gzip genisoimage; do
  command -v $cmd &>/dev/null || { echo "Missing: $cmd"; exit 1; }
done

# --- Password ---
if [[ -z "$PASSWORD" ]]; then
  read -sp "Password for user $USER: " PASSWORD
  echo
fi
PASSWORD_HASH=$(echo "$PASSWORD" | openssl passwd -6 -stdin)

# --- Download Debian ISO ---
ISO_FILE="$SCRIPT_DIR/debian-${DEBIAN_VERSION}-${ARCH}-netinst.iso"

if [[ ! -f "$ISO_FILE" ]]; then
  echo "Downloading Debian ${DEBIAN_VERSION} netinstall ISO..."
  EXACT_URL=$(curl -sL "https://cdimage.debian.org/debian-cd/current/${ARCH}/iso-cd/" | \
    grep -oP "debian-${DEBIAN_VERSION}\.[0-9]+-${ARCH}-netinst\.iso" | head -1)
  wget -q --show-progress -O "$ISO_FILE" \
    "https://cdimage.debian.org/debian-cd/current/${ARCH}/iso-cd/${EXACT_URL}"
fi

echo "Building ISO for: ${HOSTNAME} (${IP})"

# --- Extract ISO ---
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

mkdir -p "$WORK_DIR"/iso
xorriso -osirrox on -indev "$ISO_FILE" -extract / "$WORK_DIR/iso" 2>/dev/null
chmod -R u+w "$WORK_DIR/iso"

# --- Generate Preseed ---
PRESEED="$WORK_DIR/iso/preseed.cfg"
sed \
  -e "s|{{IP}}|${IP}|g" \
  -e "s|{{NETMASK}}|${NETMASK}|g" \
  -e "s|{{GATEWAY}}|${GATEWAY}|g" \
  -e "s|{{DNS}}|${DNS}|g" \
  -e "s|{{HOSTNAME}}|${HOSTNAME}|g" \
  -e "s|{{USER}}|${USER}|g" \
  -e "s|{{PASSWORD_HASH}}|${PASSWORD_HASH}|g" \
  -e "s|{{SSH_KEY}}|${SSH_KEY}|g" \
  "$SCRIPT_DIR/preseed.cfg.tpl" > "$PRESEED"

# --- Patch GRUB (UEFI) ---
GRUB_CFG="$WORK_DIR/iso/boot/grub/grub.cfg"
if [[ -f "$GRUB_CFG" ]]; then
  cat > "$GRUB_CFG" << GRUBEOF
set default=0
set timeout=0

menuentry "Debian Auto Install - ${HOSTNAME}" {
    linux /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/preseed.cfg ---
    initrd /install.amd/initrd.gz
}
GRUBEOF
fi

# --- Patch isolinux (BIOS) ---
TXT_CFG="$WORK_DIR/iso/isolinux/txt.cfg"
if [[ -f "$TXT_CFG" ]]; then
  cat > "$TXT_CFG" << TXTEOF
default auto
label auto
  menu label Auto Install - ${HOSTNAME}
  kernel /install.amd/vmlinuz
  append auto=true priority=critical preseed/file=/cdrom/preseed.cfg initrd=/install.amd/initrd.gz ---
TXTEOF
fi

ISOLINUX_CFG="$WORK_DIR/iso/isolinux/isolinux.cfg"
[[ -f "$ISOLINUX_CFG" ]] && sed -i 's/timeout .*/timeout 1/' "$ISOLINUX_CFG"

# --- Rebuild ISO ---
mkdir -p "$OUTPUT_DIR"
OUTPUT_ISO="$OUTPUT_DIR/debian-${DEBIAN_VERSION}-${HOSTNAME}-${IP}.iso"

cd "$WORK_DIR/iso"
find . -type f ! -name 'md5sum.txt' -exec md5sum {} \; > md5sum.txt 2>/dev/null || true

xorriso -as mkisofs \
  -r -J \
  -b isolinux/isolinux.bin \
  -c isolinux/boot.cat \
  -no-emul-boot \
  -boot-load-size 4 \
  -boot-info-table \
  -eltorito-alt-boot \
  -e boot/grub/efi.img \
  -no-emul-boot \
  -isohybrid-gpt-basdat \
  -o "$OUTPUT_ISO" \
  . 2>/dev/null

echo ""
echo "============================================"
echo "ISO created: $OUTPUT_ISO"
echo "Host: $HOSTNAME | IP: $IP | User: $USER"
echo "============================================"

# --- Upload + Create VM ---
if [[ -n "$PVE_HOST" ]]; then
  echo "Uploading to node${NODE} (${PVE_HOST})..."
  scp -o StrictHostKeyChecking=no "$OUTPUT_ISO" "root@${PVE_HOST}:/var/lib/vz/template/iso/"

  if [[ "$CREATE_VM" == true ]]; then
    ISO_NAME="$(basename "$OUTPUT_ISO")"
    STORAGE=$(get_storage "$NODE")
    PVE_NODE="node${NODE}"

    if [[ -z "$VMID" ]]; then
      VMID=$(ssh -o StrictHostKeyChecking=no "root@${PVE_HOST}" "pvesh get /cluster/nextid" 2>/dev/null)
      echo "Auto-assigned VMID: ${VMID}"
    fi

    echo "Creating VM ${VMID} on ${PVE_NODE}..."
    ssh -o StrictHostKeyChecking=no "root@${PVE_HOST}" "
      pvesh create /nodes/${PVE_NODE}/qemu \
        --vmid ${VMID} \
        --name ${HOSTNAME} \
        --machine q35 \
        --bios ovmf \
        --efidisk0 ${STORAGE}:1,efitype=4m,pre-enrolled-keys=0 \
        --scsihw virtio-scsi-pci \
        --scsi0 ${STORAGE}:${VM_DISK},cache=writeback \
        --ide2 local:iso/${ISO_NAME},media=cdrom \
        --net0 virtio,bridge=vmbr0 \
        --cores ${VM_CORES} \
        --memory ${VM_MEMORY} \
        --cpu cputype=host \
        --agent enabled=1 \
        --boot order='scsi0;ide2' \
        --ostype l26 \
        --onboot 1 \
        --numa 1 \
        --balloon 0 \
        --serial0 socket
    " && echo "✅ VM ${VMID} created" || { echo "❌ VM creation failed"; exit 1; }

    ssh -o StrictHostKeyChecking=no "root@${PVE_HOST}" \
      "pvesh create /nodes/${PVE_NODE}/qemu/${VMID}/status/start"
    echo "✅ VM ${VMID} started - Debian installing (~5 min)"
    echo "After install: ssh ${USER}@${IP}"
  fi
fi
