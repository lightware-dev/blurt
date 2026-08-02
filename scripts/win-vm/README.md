# Windows test VM

A scripted Windows 11 VM for testing [`clients/windows`](../../clients/windows) on a
Linux workstation, using QEMU/KVM through libvirt. Windows installs itself
unattended and lands on a logged-in desktop with `sshd` and RDP up, so nothing
here needs a human at the console.

The Mac client has no equivalent — macOS cannot legally or practically be
virtualised on non-Apple hardware, so `clients/mac` still needs real hardware.

## Why a VM at all

`clients/windows` is a tray app that registers global hotkeys, installs a
low-level keyboard hook, draws a click-through HUD and injects text with
`SendInput`. None of that can be exercised by unit tests or by CI — GitHub's
`windows-latest` runners build the binary but never run it against a real
desktop session. A local VM is the only way to see the client actually work.

## Prerequisites

```bash
sudo apt install qemu-kvm libvirt-daemon-system virtinst virt-viewer \
                 ovmf swtpm swtpm-tools xorriso
sudo usermod -aG libvirt,kvm "$USER"   # log out and back in
```

You supply the Windows installation media. A
[Windows 11 Enterprise evaluation ISO](https://www.microsoft.com/evalcenter/evaluate-windows-11-enterprise)
is the easiest option: 90 days, no key needed. Drop it in `$HOME` and the script
finds it, or point `WIN_ISO` at it.

## Usage

```bash
cd scripts/win-vm
./win-vm.sh provision
```

That generates an SSH keypair (`~/.ssh/blurt_win_vm`), bakes its public half into
the answer file, builds the answer-file ISO, defines the domain, boots the
installer and blocks until the guest answers on SSH. Expect 20–40 minutes, most
of it unattended Windows Setup. When it returns you have:

- a logged-in desktop as `blurt` / `Blurt!2026` (local admin, autologon on)
- `sshd` on port 22, key auth already set up, PowerShell as the default shell
- RDP on port 3389 with NLA off

Then:

```bash
./win-vm.sh status              # state, disk usage, IP, display URI
./win-vm.sh console             # SPICE window (run from your desktop session)
./win-vm.sh ssh                 # shell in
./win-vm.sh ssh 'Get-Process'   # or run one command
./win-vm.sh shot screen.png     # screenshot a headless guest
./win-vm.sh snapshot clean      # power down and snapshot
./win-vm.sh destroy --yes       # delete the domain, its NVRAM and its disk
```

If the guest comes up without SSH, you are not stuck — the console keyboard is a
working command channel:

```bash
./win-vm.sh run 'powershell -NoExit -Command "Get-Service sshd"'   # Win+R, type, Enter
./win-vm.sh shot                                                   # read the result
./win-vm.sh bootstrap-ssh                                          # install sshd, elevated
```

`run` drives the Win+R dialog and `type` types into whatever holds focus, so
pair them with `shot` — you are flying on screenshots, and a keystroke that goes
to the wrong window looks exactly like one that never arrived.

Take a `clean` snapshot before installing anything — you will reinstall the
client many times.

## Testing the client against a local server

`blurtd` on the host is reachable from the guest at the libvirt NAT gateway,
normally `192.168.122.1`:

```bash
./win-vm.sh ssh 'Test-NetConnection 192.168.122.1 -Port 25878'
```

Two things to get right:

- **Certificate.** [`scripts/gen_certs.sh`](../gen_certs.sh) builds the SAN list
  from a single host plus loopback, so a cert minted for `localhost` will not
  match `192.168.122.1`. Regenerate with `HOST=192.168.122.1` if you want a
  clean path; leaving it mismatched is also useful, since that is precisely the
  case the client's TOFU pinning
  ([`CertTrust.cs`](../../clients/windows/src/CertTrust.cs)) exists to handle.
- **The binary.** There is no .NET SDK on a typical Linux host, so build in the
  guest or — better, because it is what users actually get — download the
  self-contained `Blurt.exe` produced by
  [`.github/workflows/windows.yml`](../../.github/workflows/windows.yml).

Microphone input is the piece that needs the most care: the guest gets an ICH9
sound device with a PipeWire backend, so dictation testing means routing audio
into the guest's capture stream rather than relying on a physical mic.

## How it works, and what bites

**Answer file.** Windows Setup reads `autounattend.xml` from the root of any
attached removable drive. `win-vm.sh iso` packs it into its own small ISO and
attaches it as a third CD-ROM, because the install media stores its large files
in a UDF tree that `xorriso` cannot rewrite.

**The boot prompt.** UEFI Windows media stops at *"Press any key to boot from CD
or DVD"* and gives up after about five seconds, after which OVMF drops into its
boot manager. `win-vm.sh install` sends a bounded burst of keystrokes timed to
land inside that window, then confirms Setup actually started by watching the
disk grow, and retries up to three times. The burst has to be bounded —
keystrokes that arrive after the prompt expires get eaten by the boot-manager
menu instead, which leaves the VM sitting in the firmware UI.

**Boot order.** Set per device (install ISO first, disk second) rather than with
`<boot dev='hd'/>`, so post-install reboots fall through to the disk on their
own once the prompt times out.

**OOBE locale pages.** Setting locales only in the `windowsPE` pass is not
enough: OOBE asks *"Is this the right country or region?"* again and the install
stalls there forever. `Microsoft-Windows-International-Core` must also appear in
the `oobeSystem` pass.

**A failing `specialize` command kills the install.** If any
`RunSynchronousCommand` exits non-zero, Windows Setup aborts with *"The computer
restarted unexpectedly or encountered an unexpected error"* and the install
cannot be resumed — you rebuild from scratch. `powershell.exe` exits 1 on a
merely non-terminating error, so this is very easy to trip: `Start-Service sshd`
when the OpenSSH capability hasn't installed is enough to do it. On top of that,
`<Path>` is capped at 259 characters. That is why the real work lives in
[`setup-guest.ps1`](setup-guest.ps1) on the answer ISO, which is best-effort
throughout and ends with `exit 0`, and why `specialize` runs exactly one
command. It logs to `C:\Windows\Temp\blurt-setup.log` in the guest.

**`New-Item -Force` deletes registry keys.** In PowerShell's registry provider
`-Force` does not mean "create if missing" — it recreates the key, destroying
every subkey and value. Doing that to
`HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE` wipes the OOBE plugin
registrations, and the install dies with *"Windows could not complete the
installation"* and `Failed to open OOBE plugin key [hr=0x80070002]` in
`C:\Windows\Panther\UnattendGC\setupact.log`. `setup-guest.ps1` uses a
`Test-Path`-guarded helper instead. (`New-Item -ItemType Directory -Force` on the
filesystem is fine — this is a registry-provider quirk.)

**Network-dependent work cannot go in `specialize`.** That pass runs before
Windows Update is properly up, so `Add-WindowsCapability` for a Feature on
Demand blocks indefinitely and Setup hangs on a DISM progress bar that never
advances. Installing OpenSSH is therefore deferred to a SYSTEM scheduled task
that fires at startup and unregisters itself once `sshd` is running.

**Diagnosing a failed install.** Shift+F10 at any Setup screen opens an
Administrator command prompt — the only way in when the install has stopped:

```bash
virsh -c qemu:///system send-key win11-blurt-test --codeset linux KEY_LEFTSHIFT KEY_F10
./win-vm.sh shot                      # confirm cmd has focus before typing
./win-vm.sh type 'type C:\Windows\Temp\blurt-setup.log'
```

Watch the focus: an error dialog behind the console will happily take your
Enter, and its default button restarts the machine.

**Elevation.** Windows 11 runs `FirstLogonCommands` with a UAC-filtered token
even for an account in Administrators, so `Add-WindowsCapability`, `HKLM` writes
and `netsh` all fail there with *"The requested operation requires elevation"* —
silently, leaving a VM that boots to a desktop with no way in. Everything that
needs admin rights therefore lives in the `specialize` pass, which runs as
SYSTEM.

**The SSH firewall rule.** Installing the OpenSSH Server capability does *not*
open port 22. The service ends up `Running` while the port stays closed, which
from the host is indistinguishable from a failed install. The answer file adds
the rule explicitly.

**UAC's default button is "No".** If you ever drive a consent prompt from the
keyboard, a bare Enter *declines* it, and nothing on screen says so. `win-vm.sh
uac` moves left to "Yes" first.

**Firmware.** Windows 11 requires secure boot and a TPM, so the domain uses
`OVMF_CODE_4M.ms.fd` (Microsoft keys enrolled), `smm` on, and an emulated
TPM 2.0. Disk and NIC are SATA and `e1000e` so no virtio driver ISO is needed
during install.

**Audio.** The domain passes `XDG_RUNTIME_DIR` through to QEMU so the PipeWire
backend can reach your user session's socket. Without it the guest boots with a
sound device that produces nothing.

## Configuration

Every setting is an environment variable with a sensible default — see
`./win-vm.sh help`. The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `VM_NAME` | `win11-blurt-test` | libvirt domain name |
| `WIN_ISO` | newest `*CLIENTENTERPRISEEVAL*.iso` in `$HOME` | install media |
| `VM_RAM_MB` / `VM_VCPUS` | `8192` / `4` | Windows 11 minimum is 4 GB |
| `VM_DISK_GB` | `100` | thin-provisioned; a fresh install uses ~15 GB |
| `GUEST_USER` / `GUEST_PASS` | `blurt` / `Blurt!2026` | must match `autounattend.xml` |

The credentials are weak deliberately: this is a disposable VM on an isolated
NAT network, rebuilt from scratch whenever it breaks. Do not reuse this pattern
for anything reachable from outside the host.
