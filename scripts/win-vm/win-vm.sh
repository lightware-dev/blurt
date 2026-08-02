#!/usr/bin/env bash
#
# Build and drive the Windows 11 VM used to test the Windows tray client on a
# Linux workstation, using QEMU/KVM through libvirt.
#
# The whole point is that no step needs a human at the console: the VM installs
# Windows unattended and lands on a logged-in desktop with sshd and RDP up, so
# the rest of the client testing can be scripted.
#
#   ./win-vm.sh provision      # one shot: iso -> create -> install -> wait
#   ./win-vm.sh console        # open a SPICE window on your desktop session
#   ./win-vm.sh ssh            # shell into the guest once it is ready
#
# Run `./win-vm.sh help` for the full command list.
#
# Requires: virt-install, virsh, qemu-kvm, xorriso, and membership of the
# libvirt and kvm groups. You supply the Windows installation ISO.

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Configuration (override any of these in the environment)
# ---------------------------------------------------------------------------

LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
VM_NAME="${VM_NAME:-win11-blurt-test}"
VM_RAM_MB="${VM_RAM_MB:-8192}"
VM_VCPUS="${VM_VCPUS:-4}"
VM_DISK_GB="${VM_DISK_GB:-100}"
VM_POOL="${VM_POOL:-default}"
NET_NAME="${NET_NAME:-default}"

# Windows installation media. Defaults to the newest Enterprise Evaluation ISO
# in $HOME, which is what the download from Microsoft's evaluation centre is
# named. Set WIN_ISO explicitly for anything else.
WIN_ISO="${WIN_ISO:-}"

# The generated answer-file ISO. Lives outside the repo because libvirt-qemu
# has to be able to read it, and it is a build artifact, not source.
UNATTEND_ISO="${UNATTEND_ISO:-$HOME/blurt-unattend.iso}"

# Secure-boot firmware with Microsoft's keys enrolled. Windows 11 refuses to
# install without secure boot and a TPM.
OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.ms.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.ms.fd}"

# Credentials created by autounattend.xml. Weak on purpose: throwaway VM on an
# isolated NAT network.
GUEST_USER="${GUEST_USER:-blurt}"
GUEST_PASS="${GUEST_PASS:-Blurt!2026}"

# Dedicated SSH identity for the VM, generated on first `iso` build and baked
# into the answer file so the guest is key-reachable the moment it boots.
SSH_KEY="${SSH_KEY:-$HOME/.ssh/blurt_win_vm}"

readonly VOL_NAME="${VM_NAME}.qcow2"
readonly DISK_TARGET="sda"   # first SATA disk, per the --disk order in cmd_create

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m error:\033[0m %s\n' "$*" >&2; exit 1; }

v() { virsh --connect "$LIBVIRT_URI" "$@"; }

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is not installed"
}

domain_exists() { v dominfo "$VM_NAME" >/dev/null 2>&1; }
domain_running() { [[ "$(v domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')" == "running" ]]; }

resolve_win_iso() {
    if [[ -n "$WIN_ISO" ]]; then
        [[ -f "$WIN_ISO" ]] || die "WIN_ISO does not exist: $WIN_ISO"
        return
    fi
    # Newest *CLIENTENTERPRISEEVAL*.iso in $HOME, else any single .iso there.
    WIN_ISO="$(ls -t "$HOME"/*CLIENTENTERPRISEEVAL*.iso 2>/dev/null | head -1 || true)"
    [[ -n "$WIN_ISO" ]] || die "no Windows ISO found in \$HOME; set WIN_ISO=/path/to/windows.iso"
    log "using Windows media: $WIN_ISO"
}

# Allocation of the guest disk in MiB. This is the only progress signal a
# headless install gives us, so both install and wait lean on it.
disk_alloc_mib() {
    local bytes
    bytes="$(v domblkinfo "$VM_NAME" "$DISK_TARGET" 2>/dev/null \
        | awk '/^Allocation:/ {print $2}')"
    [[ -n "${bytes:-}" ]] || { echo 0; return; }
    echo $(( bytes / 1024 / 1024 ))
}

guest_mac() {
    v dumpxml "$VM_NAME" 2>/dev/null \
        | sed -n "s/.*<mac address='\([^']*\)'.*/\1/p" | head -1
}

guest_ip() {
    local mac
    mac="$(guest_mac)"
    [[ -n "$mac" ]] || return 0
    v net-dhcp-leases "$NET_NAME" 2>/dev/null \
        | awk -v m="$mac" 'tolower($0) ~ tolower(m) {print $5}' \
        | cut -d/ -f1 | head -1
}

port_open() {
    timeout 3 bash -c "echo > /dev/tcp/$1/$2" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_iso() {
    need xorriso
    local staging
    staging="$(mktemp -d)"

    # A dedicated key, so the guest is reachable without a password and without
    # touching the user's everyday SSH identity.
    if [[ ! -f "$SSH_KEY" ]]; then
        log "generating $SSH_KEY"
        ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -C "blurt-win-vm" >/dev/null
    fi

    cp "$SCRIPT_DIR/autounattend.xml" "$staging/autounattend.xml"
    cp "$SCRIPT_DIR/setup-guest.ps1" "$staging/setup-guest.ps1"
    # '|' as the delimiter: base64 key material contains / and + but never |.
    sed -i "s|@@AUTHORIZED_KEY@@|$(cat "$SSH_KEY.pub")|" "$staging/setup-guest.ps1"
    # Sanity-check before burning it: a malformed answer file shows up as a VM
    # that silently sits on a Setup page, which is miserable to debug.
    python3 -c "import xml.dom.minidom,sys; xml.dom.minidom.parse(sys.argv[1])" \
        "$staging/autounattend.xml" || die "autounattend.xml is not well-formed XML"

    log "building $UNATTEND_ISO"
    xorriso -as mkisofs -V UNATTEND -J -r -o "$UNATTEND_ISO" "$staging" >/dev/null 2>&1
    chmod a+r "$UNATTEND_ISO"
    rm -rf "$staging"
    log "built $(du -h "$UNATTEND_ISO" | cut -f1) answer-file ISO"
}

cmd_create() {
    need virt-install
    resolve_win_iso
    [[ -f "$UNATTEND_ISO" ]] || die "answer ISO missing; run '$0 iso' first"
    [[ -f "$OVMF_CODE" ]] || die "secure-boot firmware not found: $OVMF_CODE (install ovmf)"

    if domain_exists; then
        die "domain $VM_NAME already exists; '$0 destroy --yes' first"
    fi

    # Create the disk through the libvirt pool rather than qemu-img, so this
    # works without root even though the pool lives under /var/lib/libvirt.
    if ! v vol-info --pool "$VM_POOL" "$VOL_NAME" >/dev/null 2>&1; then
        log "creating ${VM_DISK_GB}G disk in pool $VM_POOL"
        v vol-create-as "$VM_POOL" "$VOL_NAME" "${VM_DISK_GB}G" --format qcow2 >/dev/null
    fi
    local disk_path
    disk_path="$(v vol-path --pool "$VM_POOL" "$VOL_NAME")"

    local xml
    xml="$(mktemp)"

    # --print-xml + virsh define instead of letting virt-install boot the VM:
    # the install has to start under cmd_install, which drives the keyboard.
    #
    # Boot order is set per device (install ISO first, disk second) rather than
    # with <boot dev=...>, so that post-install reboots fall through to the disk
    # on their own once the "press any key" prompt times out.
    log "defining domain $VM_NAME"
    virt-install \
        --connect "$LIBVIRT_URI" \
        --name "$VM_NAME" \
        --osinfo win11 \
        --memory "$VM_RAM_MB" \
        --vcpus "$VM_VCPUS" \
        --cpu host-passthrough \
        --machine q35 \
        --features smm.state=on \
        --boot "loader=$OVMF_CODE,loader.readonly=yes,loader.type=pflash,loader.secure=yes,nvram.template=$OVMF_VARS" \
        --tpm model=tpm-crb,backend.type=emulator,backend.version=2.0 \
        --disk "path=$disk_path,format=qcow2,bus=sata,discard=unmap,boot.order=2" \
        --disk "path=$WIN_ISO,device=cdrom,bus=sata,readonly=on,boot.order=1" \
        --disk "path=$UNATTEND_ISO,device=cdrom,bus=sata,readonly=on" \
        --network "network=$NET_NAME,model=e1000e" \
        --graphics spice \
        --video qxl \
        --sound model=ich9 \
        --audio type=pipewire \
        --channel spicevmc \
        --qemu-commandline "env=XDG_RUNTIME_DIR=/run/user/$(id -u)" \
        --noautoconsole \
        --print-xml > "$xml"

    # virt-install prints <audio type="pipewire"/> without the id attribute that
    # libvirt requires on define - it only fills that in when it defines the
    # domain itself, which we deliberately do not let it do.
    grep -q '<audio id=' "$xml" || sed -i 's|<audio |<audio id="1" |' "$xml"

    v define "$xml" >/dev/null
    rm -f "$xml"
    log "domain defined"
}

# Boot the install media.
#
# UEFI Windows media stops at "Press any key to boot from CD or DVD" and gives
# up after about five seconds, at which point OVMF falls through to its boot
# manager. Nothing presses that key on a headless host, so we send a short burst
# of keystrokes timed to land inside that window. The burst must be bounded:
# keys that arrive after the prompt expires get eaten by the boot-manager menu
# instead, which is how this fails when you simply spam the keyboard.
cmd_install() {
    domain_exists || die "domain $VM_NAME does not exist; run '$0 create' first"

    local attempt
    for attempt in 1 2 3; do
        log "boot attempt $attempt/3"
        v destroy "$VM_NAME" >/dev/null 2>&1 || true
        sleep 3
        v start "$VM_NAME" >/dev/null

        local i
        for ((i = 0; i < 24; i++)); do
            sleep 0.7
            v send-key "$VM_NAME" --codeset linux KEY_SPACE >/dev/null 2>&1 || true
        done

        # Setup partitions and starts applying the image within a couple of
        # minutes; a disk that stays empty means we missed the prompt.
        log "checking whether Setup started"
        local waited alloc
        for ((waited = 0; waited < 240; waited += 15)); do
            sleep 15
            alloc="$(disk_alloc_mib)"
            if (( alloc > 200 )); then
                log "Setup is installing (${alloc} MiB written)"
                return 0
            fi
        done
        warn "no disk activity after boot attempt $attempt - missed the boot prompt"
    done

    die "could not boot the install media; open '$0 console' and press a key at the prompt yourself"
}

# Block until the guest is actually usable. Windows reboots several times, and
# the specialize pass pulls the OpenSSH feature down from Windows Update before
# the desktop ever appears, so this legitimately takes a while.
cmd_wait() {
    local deadline=$(( SECONDS + ${WAIT_TIMEOUT:-3600} ))
    local ip="" reported_rdp=0 prev_alloc=0 alloc

    log "waiting for the guest to finish installing"
    while (( SECONDS < deadline )); do
        if ! domain_running; then
            die "VM is not running (state: $(v domstate "$VM_NAME" 2>/dev/null))"
        fi

        alloc="$(disk_alloc_mib)"
        if (( alloc > prev_alloc + 3000 )); then
            log "installing: ${alloc} MiB written"
            prev_alloc="$alloc"
        fi

        ip="$(guest_ip)"
        if [[ -n "$ip" ]]; then
            if port_open "$ip" 22; then
                log "guest ready: ssh $GUEST_USER@$ip"
                echo "$ip"
                return 0
            fi
            if (( ! reported_rdp )) && port_open "$ip" 3389; then
                log "Windows is up at $ip (RDP answering, still waiting on sshd)"
                reported_rdp=1
            fi
        fi
        sleep 20
    done

    warn "timed out waiting for sshd"
    [[ -n "$ip" ]] && warn "guest is at $ip - try '$0 rdp', sshd may have failed to install"
    return 1
}

cmd_provision() {
    cmd_iso
    cmd_create
    cmd_install
    cmd_wait
}

cmd_ip() {
    local ip
    ip="$(guest_ip)"
    [[ -n "$ip" ]] || die "no DHCP lease for $VM_NAME yet"
    echo "$ip"
}

cmd_ssh() {
    local ip
    ip="$(cmd_ip)"
    local -a opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
    if [[ -f "$SSH_KEY" ]]; then
        opts+=(-i "$SSH_KEY")
    else
        log "no $SSH_KEY; password is $GUEST_PASS"
    fi
    exec ssh "${opts[@]}" "$GUEST_USER@$ip" "$@"
}

cmd_rdp() {
    local ip bin
    ip="$(cmd_ip)"
    bin="$(command -v xfreerdp3 || command -v xfreerdp || true)"
    [[ -n "$bin" ]] || die "no xfreerdp found (apt install freerdp3-x11)"
    exec "$bin" "/v:$ip" "/u:$GUEST_USER" "/p:$GUEST_PASS" /cert:ignore /dynamic-resolution
}

cmd_console() {
    need virt-viewer
    # --reconnect matters: Windows Setup reboots several times and the viewer
    # window otherwise dies on the first one.
    exec virt-viewer --connect "$LIBVIRT_URI" --attach --reconnect "$VM_NAME"
}

# Screenshot the guest console. The only way to see what a headless install is
# doing, and the fastest way to tell a stalled Setup page from a slow one.
cmd_shot() {
    local out="${1:-win-vm-$(date +%Y%m%d-%H%M%S).png}"
    local raw
    raw="$(mktemp --suffix=.img)"
    v screenshot "$VM_NAME" --file "$raw" >/dev/null
    if command -v python3 >/dev/null && python3 -c "import PIL" 2>/dev/null; then
        python3 -c "from PIL import Image; Image.open('$raw').convert('RGB').save('$out')"
    else
        cp "$raw" "$out"
    fi
    rm -f "$raw"
    log "wrote $out"
}

# Map one character to a `virsh send-key` argument list, US layout. Enough of
# ASCII to type a PowerShell one-liner; anything else is skipped with a warning.
keycode_for() {
    local c="$1"
    case "$c" in
        [a-z])  echo "KEY_${c^^}" ;;
        [A-Z])  echo "KEY_LEFTSHIFT KEY_${c}" ;;
        [0-9])  echo "KEY_${c}" ;;
        ' ')    echo "KEY_SPACE" ;;
        '-')    echo "KEY_MINUS" ;;
        '_')    echo "KEY_LEFTSHIFT KEY_MINUS" ;;
        '=')    echo "KEY_EQUAL" ;;
        '+')    echo "KEY_LEFTSHIFT KEY_EQUAL" ;;
        '.')    echo "KEY_DOT" ;;
        ',')    echo "KEY_COMMA" ;;
        ';')    echo "KEY_SEMICOLON" ;;
        ':')    echo "KEY_LEFTSHIFT KEY_SEMICOLON" ;;
        "'")    echo "KEY_APOSTROPHE" ;;
        '"')    echo "KEY_LEFTSHIFT KEY_APOSTROPHE" ;;
        '/')    echo "KEY_SLASH" ;;
        '?')    echo "KEY_LEFTSHIFT KEY_SLASH" ;;
        '\')    echo "KEY_BACKSLASH" ;;
        '|')    echo "KEY_LEFTSHIFT KEY_BACKSLASH" ;;
        '`')    echo "KEY_GRAVE" ;;
        '~')    echo "KEY_LEFTSHIFT KEY_GRAVE" ;;
        '[')    echo "KEY_LEFTBRACE" ;;
        ']')    echo "KEY_RIGHTBRACE" ;;
        '{')    echo "KEY_LEFTSHIFT KEY_LEFTBRACE" ;;
        '}')    echo "KEY_LEFTSHIFT KEY_RIGHTBRACE" ;;
        '!')    echo "KEY_LEFTSHIFT KEY_1" ;;
        '@')    echo "KEY_LEFTSHIFT KEY_2" ;;
        '#')    echo "KEY_LEFTSHIFT KEY_3" ;;
        '$')    echo "KEY_LEFTSHIFT KEY_4" ;;
        '%')    echo "KEY_LEFTSHIFT KEY_5" ;;
        '^')    echo "KEY_LEFTSHIFT KEY_6" ;;
        '&')    echo "KEY_LEFTSHIFT KEY_7" ;;
        '*')    echo "KEY_LEFTSHIFT KEY_8" ;;
        '(')    echo "KEY_LEFTSHIFT KEY_9" ;;
        ')')    echo "KEY_LEFTSHIFT KEY_0" ;;
        '<')    echo "KEY_LEFTSHIFT KEY_COMMA" ;;
        '>')    echo "KEY_LEFTSHIFT KEY_DOT" ;;
        *)      return 1 ;;
    esac
}

# Type a string into the guest console, one keystroke at a time.
cmd_type() {
    local text="$*"
    local i c keys
    for ((i = 0; i < ${#text}; i++)); do
        c="${text:i:1}"
        if keys="$(keycode_for "$c")"; then
            # shellcheck disable=SC2086  # deliberate word splitting
            v send-key "$VM_NAME" --codeset linux $keys >/dev/null
        else
            warn "cannot type character: '$c'"
        fi
    done
}

# Run a command line in the guest through the Win+R dialog.
#
# This is the bootstrap channel. Before sshd exists the console keyboard is the
# only way in, and it is also how you diagnose a guest whose first-logon
# commands failed - run the failing command with -NoExit and read the error off
# a screenshot.
cmd_run() {
    local cmdline="$*"
    [[ -n "$cmdline" ]] || die "usage: $0 run <command line>"
    domain_running || die "$VM_NAME is not running"

    v send-key "$VM_NAME" --codeset linux KEY_ESC >/dev/null   # dismiss Start etc.
    sleep 1
    v send-key "$VM_NAME" --codeset linux KEY_LEFTMETA KEY_R >/dev/null
    sleep 2
    cmd_type "$cmdline"
    sleep 1
    v send-key "$VM_NAME" --codeset linux KEY_ENTER >/dev/null
    log "sent: $cmdline"
}

# Confirm a UAC consent dialog.
#
# Windows 11 focuses "No", not "Yes", so a bare Enter DECLINES the prompt - and
# it does so silently, which reads exactly like the keystroke never arrived.
# Move left to "Yes" first.
cmd_uac() {
    v send-key "$VM_NAME" --codeset linux KEY_LEFT >/dev/null
    sleep 1
    v send-key "$VM_NAME" --codeset linux KEY_ENTER >/dev/null
    log "sent UAC confirmation"
}

# Install and start the OpenSSH server from the console keyboard.
#
# autounattend.xml already tries this, but the install needs a full
# administrator token and Windows 11 runs FirstLogonCommands with a UAC-filtered
# one, so it fails with "The requested operation requires elevation". This
# re-runs it through Start-Process -Verb RunAs and confirms the consent prompt.
cmd_bootstrap_ssh() {
    domain_running || die "$VM_NAME is not running"

    # The firewall rule matters as much as the capability: without it sshd runs
    # happily while port 22 stays shut, which looks identical to a failed install.
    local inner="Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Set-Service sshd -StartupType Automatic; Start-Service sshd; New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22"
    cmd_run "powershell -NoProfile -Command \"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-NoExit','-Command','$inner'\""

    log "waiting for the UAC prompt"
    sleep 6
    cmd_uac

    log "installing OpenSSH in the guest (pulled from Windows Update, give it a few minutes)"
    local ip waited
    ip="$(guest_ip)"
    for ((waited = 0; waited < 600; waited += 15)); do
        sleep 15
        if [[ -n "$ip" ]] && port_open "$ip" 22; then
            log "sshd is up: ssh $GUEST_USER@$ip"
            return 0
        fi
    done
    warn "sshd still not answering; run '$0 shot' to see what the guest is showing"
    return 1
}

cmd_snapshot() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "usage: $0 snapshot <name>"
    log "taking snapshot '$name' (this powers the VM down first)"
    if domain_running; then
        v shutdown "$VM_NAME" >/dev/null 2>&1 || true
        local waited
        for ((waited = 0; waited < 120; waited += 5)); do
            domain_running || break
            sleep 5
        done
        # Windows ignoring the ACPI request is normal; the disk is consistent
        # enough for a scratch VM either way.
        if domain_running; then
            warn "guest did not shut down cleanly, pulling the plug"
            v destroy "$VM_NAME" >/dev/null 2>&1 || true
        fi
    fi
    v snapshot-create-as "$VM_NAME" "$name" --disk-only --atomic 2>/dev/null \
        || v snapshot-create-as "$VM_NAME" "$name"
    log "snapshot '$name' created"
}

cmd_status() {
    if ! domain_exists; then
        echo "domain:  $VM_NAME (not defined)"
        return
    fi
    echo "domain:  $VM_NAME"
    echo "state:   $(v domstate "$VM_NAME")"
    echo "disk:    $(disk_alloc_mib) MiB allocated"
    echo "mac:     $(guest_mac)"
    echo "ip:      $(guest_ip || echo '(no lease)')"
    echo "display: $(v domdisplay "$VM_NAME" 2>/dev/null || echo '(not running)')"
}

cmd_destroy() {
    [[ "${1:-}" == "--yes" ]] || die "this deletes the VM and its disk; pass --yes to confirm"
    domain_exists || die "domain $VM_NAME does not exist"
    log "destroying $VM_NAME and its disk"
    v destroy "$VM_NAME" >/dev/null 2>&1 || true
    v undefine "$VM_NAME" --nvram --snapshots-metadata >/dev/null 2>&1 \
        || v undefine "$VM_NAME" --nvram >/dev/null
    v vol-delete --pool "$VM_POOL" "$VOL_NAME" >/dev/null 2>&1 || true
    log "gone"
}

cmd_help() {
    cat <<EOF
Usage: $(basename "$0") <command> [args]

Setup
  provision        iso + create + install + wait, end to end
  iso              build the autounattend answer-file ISO
  create           define the libvirt domain and its disk
  install          boot the install media and drive the boot prompt
  wait             block until the guest is installed and sshd answers

Use
  status           domain state, disk usage, IP, display URI
  ip               print the guest's DHCP address
  ssh [cmd...]     ssh into the guest as $GUEST_USER
  rdp              open an RDP session (xfreerdp)
  console          open the SPICE console (virt-viewer)
  shot [file.png]  screenshot the guest console

Console keyboard (works before sshd exists)
  run <cmdline>    run a command line via the guest's Win+R dialog
  type <text>      type a string into the guest console
  uac              confirm a UAC consent dialog
  bootstrap-ssh    install and start sshd (elevated) when first logon failed

Manage
  snapshot <name>  power down and snapshot
  destroy --yes    delete the domain, its NVRAM and its disk

Environment: LIBVIRT_URI VM_NAME VM_RAM_MB VM_VCPUS VM_DISK_GB VM_POOL
             NET_NAME WIN_ISO UNATTEND_ISO OVMF_CODE OVMF_VARS
             GUEST_USER GUEST_PASS WAIT_TIMEOUT
EOF
}

# ---------------------------------------------------------------------------

main() {
    need virsh
    local cmd="${1:-help}"
    shift || true
    case "$cmd" in
        provision|iso|create|install|wait|ip|ssh|rdp|console|shot|snapshot|status|destroy|help|type|run|uac)
            "cmd_$cmd" "$@"
            ;;
        bootstrap-ssh)
            cmd_bootstrap_ssh "$@"
            ;;
        *)
            die "unknown command '$cmd' (try '$0 help')"
            ;;
    esac
}

main "$@"
