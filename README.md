# Evil_StepBROTHER

**Stop fighting with the QL-810Wc forgetting its network configuration: buy a
Raspberry Pi, install Fedora, and run this script.**

Evil_StepBROTHER turns a small Fedora 44 Server machine (called the box
below) with a Brother QL-810Wc label printer on USB into what Brother's
software takes for a networked QL-810W. P-touch Editor for Mac finds the box
on the network, shows the roll that is loaded and prints to it in black or
in black and red. Brother iPrint&Label for iOS finds the box and prints to
it, and its **Check Media** reads the loaded roll. The printer only needs
its USB cable.

On the box the software keeps its technical names: `ql810wc-print-proxy` in
its messages and in the one-liner's checkout
(`/usr/local/src/ql810wc-print-proxy`), `ql810wc-proxyd` and
`ql810wc-snmp-agent` for its services, and `ql810wc-proxy` in its other
paths, such as `/etc/ql810wc-proxy` ([Where things live](#where-things-live)).

Not affiliated with or endorsed by Brother Industries, Ltd. "Brother",
"P-touch", "iPrint&Label" and "QL-810W" are trademarks of their respective
owners and are used here only to describe compatibility. Raspberry Pi is a
trademark of Raspberry Pi Ltd. Fedora, Mac, macOS, iPhone, iOS, Bonjour,
AirPrint, Windows, Android and the other product names used here are
trademarks of their respective owners; this project is not affiliated with
or endorsed by any of them.

## Quick start

You need:

- a Brother QL-810Wc and the USB cable that came with it (the plain QL-810W
  is expected to work too, but is untested);
- a Raspberry Pi 4 with a power supply and a microSD card of 16 GB or more,
  or any other 64-bit machine (x86_64 or aarch64) that runs Fedora 44
  Server;
- a network connection for the box, on the same network (subnet) as your
  Mac or iPhone: Bonjour discovery does not cross routers.

1. Install Fedora 44 Server and log in over SSH:
   [docs/fedora-on-raspberry-pi.md](docs/fedora-on-raspberry-pi.md). Fedora's
   download page offers only the newest release, which is not tested with
   this project; the guide links the Fedora 44 images. Give the box a host
   name: the printer appears under it.
2. Connect the printer to the box by USB and switch it on. **Editor Lite
   must be off** (green LED dark): press and hold the Editor Lite button
   until the LED goes out.
3. On the box, run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
   ```

   It installs `git-core` when git is missing (a fresh Fedora Server has
   none), clones this repository into
   `/usr/local/src/ql810wc-print-proxy`, writes `config.env` there (the
   client network is the box's own IPv4 network), installs everything and
   ends with the selftest. With a DK-22251 roll (62 mm black/red) loaded,
   a working install reports `TIER REACHED: T3`; with any other roll it
   reports `T0`, which is enough to print ([Selftest](#selftest)). setup.sh
   then says what to do next. Options go after `bash -s --`, for example:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash -s -- --clients 192.0.2.0/24 --name office
   ```

   `192.0.2.0/24` is a documentation-only example: replace it with your
   clients' network in CIDR form, or leave `--clients` out to use the box's
   own network.

4. On a Mac, open P-touch Editor 5.3 (from the Mac App Store) and select
   **`Brother QL-810W [<name>]`**: `<name>` is the box's host name, or what
   you passed to `--name`. On an iPhone, Brother iPrint&Label (from the App
   Store) finds the box; it starts with a 1.4" label selected, so use its
   **Check Media** to switch to the roll that is loaded. Details for each
   client, including the media rules for the black/red roll:
   [docs/client-setup.md](docs/client-setup.md).

If setup.sh stops with exit status 3, a person must act first (for example
switch the printer on, or turn Editor Lite off); the message says what, and
what to run again.

To upgrade later, run the one-liner again without options: it updates its
checkout and keeps that checkout's `config.env`. Options you give again set
their settings again, replacing any change you made to those settings in
`config.env` since. A box installed from a checkout of its own is upgraded
from that checkout instead: on such a box the one-liner stops with exit
status 3 and says what to run ([Upgrade](#upgrade)).

Without piping a script into `sudo bash`:

```bash
sudo dnf -y install git-core
git clone https://github.com/GhostCode-IO/Evil_StepBROTHER.git
cd Evil_StepBROTHER
sudo ./setup.sh
```

## What works

Tested on a Raspberry Pi 4 running Fedora 44 Server (aarch64, SELinux
enforcing, net-snmp 5.9.5.2) with a QL-810Wc on USB.

| Client | Finds the box | Shows the loaded roll | Prints |
|---|---|---|---|
| P-touch Editor 5.3 for Mac | verified with 5.3.23 (Bonjour) | verified: DK-22251 shown as 62 mm black/red | verified: black and black/red labels, checked by eye |
| Brother iPrint&Label for iOS | verified (2026-11-21) | verified (2026-11-21), after the app's **Check Media** | verified (2026-11-21) |
| Other Mac apps (for example PowerPoint) with Brother's QL-810W driver for macOS | verified (2026-11-21): macOS adds the box from Bonjour | not applicable: you choose the label size | partly verified (2026-11-21): the driver's black/red output, sent from a Mac straight to TCP 9100, printed correctly (checked by eye); its monochrome output is refused. Not yet through a macOS print queue |
| P-touch Editor for Windows with Brother's QL-810W(c) driver | untested | untested | untested |
| BRAdmin | untested | untested | not applicable |
| Android apps (iPrint&Label and others) | untested | untested | untested |

`selftest.sh` grades an installation in tiers that build on each other: T0
printing over TCP 9100, T1 black/red printing, T2 discovery (SNMP and
Bonjour), T3 the loaded roll reported live, and the bonus `+host SNMP` when
SNMPv3 monitoring of the box works too ([Selftest](#selftest)).

Also verified on the tested unit:

- `selftest.sh` reports `TIER REACHED: T3 (+host SNMP)` after a fresh
  install, after an in-place upgrade, and after `uninstall.sh` followed by a
  reinstall. A second install run changes nothing and restarts nothing.
- The one-liner, piped into `sudo bash` on the tested unit (installed from a
  checkout of its own, no git). It installed `git-core`, cloned into
  `/usr/local/src/ql810wc-print-proxy` (from a git bundle of a pre-release
  commit, as the repository was not public yet), then stopped with exit
  status 3 and named the `config.env` the box was installed with. With that
  file copied into the new checkout, the next run fast-forwarded the
  checkout, kept `config.env`, ran the install and reached
  `TIER REACHED: T3 (+host SNMP)` with the same printer identity. A further
  run changed nothing.
- iPrint&Label for iOS reads the loaded roll from the box with its
  **Check Media** command. The app starts out with a 1.4" label size and
  does not read the roll by itself, so use **Check Media** after selecting
  the printer. That is how the app works, not a limitation of the box.
- macOS adds the box as a printer straight from its Bonjour record, with
  Brother's QL-810W printer driver for macOS, so applications such as
  PowerPoint can send it jobs. Before `ql810wc-proxyd` had its colour check
  (`RAW_COLOR_CHECK`), a job from PowerPoint reached the printer. It was
  monochrome, the driver's default, so with DK-22251 loaded the printer
  rejected it and stopped answering until it was switched off and on (see
  the [media rules](docs/client-setup.md#media-rules)). For this roll, set
  the driver's **Color** option to **2-Color(Black/Red)**.
- The colour check, with DK-22251 loaded: a label rendered by Brother's
  QL-810W driver for macOS (its own filter, run by hand on a Mac) was sent
  from the Mac to TCP 9100. The Monochrome version was refused: the Mac's
  connection was reset, `qlctl status` showed the job as `refused` with the
  reason, and the printer kept answering, with no error and no power cycle.
  The 2-Color(Black/Red) version printed in black and red where it should
  (checked by eye). `qlctl test-print --mono --force` was refused the same
  way and exited with 3.
- P-touch Editor for Mac sends a short trailer after each job.
  `ql810wc-proxyd` holds it until the printer has finished the label, then
  forwards it; P-touch reports success, and the black and black/red labels
  come out right (checked by eye).
- The 3 s idle status poll keeps the printer on: it stayed on for almost four
  hours without a print job. Without polls it switches itself off about 60
  minutes after the last status request.
- `systemctl reload ql810wc-snmp-agent`, which the installer uses when only
  settings or the OID map change, keeps the agent's AgentX session: all 135
  status-block GETs sent during two reloads were answered.
- The SNMP split: the v1/v2c communities `public` and `internal` see only the
  printer persona (none of the host's data such as the hrStorage, hrSWRun or
  UCD tables), a wrong community gets no answer, and SNMPv3 authPriv sees the
  host in the default context and the persona with `-n printer`.

**Not verified:**

- Windows: P-touch Editor for Windows, Brother's QL-810W(c) driver and its
  installer's network search, and the SNMP status of Windows' Standard
  TCP/IP port.
- BRAdmin, and Android apps.
- A black/red label printed from another Mac application through a macOS
  print queue with Brother's QL-810W driver (**Color** set to
  **2-Color(Black/Red)**). The driver's own output, sent to TCP 9100, printed.
- The plain QL-810W (firmware 1.x). It reports the same USB ID and protocol,
  so it is expected to work.
- Die-cut DK-11201 labels printed from P-touch, and P-touch noticing a roll
  swap. The printer's status for DK-11201 was verified over USB.
- The persona's identity strings. `sysDescr`, `sysObjectID`, `ifDescr`,
  `brpsHardwareType` and the other entries marked `verified: false` in
  [`agent/oid-map.yaml`](agent/oid-map.yaml) are placeholders taken from
  other Brother devices. Clients that check them may not recognise the box.
  Contributors who can put a QL-810Wc on Wi-Fi can check them with the tools
  in [`dev/snmpwalk/`](dev/snmpwalk/README.md).
- An install with a printer on x86_64. CI runs the test suite on Fedora 44
  x86_64.
- Fedora 45 and later releases.
- `setup.sh` on a freshly installed Fedora 44 with a printer attached. On
  the tested unit the one-liner upgraded an existing install (see above).
  CI runs its bootstrap in a fresh Fedora 44 container, without systemd or
  a printer ([Development](#development)).

## setup.sh reference

```bash
curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash -s -- --clients 192.0.2.0/24 --name office
sudo ./setup.sh [options] [-- install.sh options]    # from a checkout
```

`192.0.2.0/24` stands for your clients' network (see `--clients` below).
Running the same command again upgrades the installation: every step is
idempotent, and `config.env` is kept except for the settings named by the
options you pass ([Upgrade](#upgrade), which also covers a box installed
from a checkout of your own). `./setup.sh --help` shows these forms and the
options below. setup.sh asks no questions. When its standard input is not a
terminal (as in `curl | sudo bash`), the commands it runs read nothing from
it.

setup.sh works in two modes:

- **Bootstrap** (piped into bash, or a setup.sh without `install.sh` and
  `scripts/lib/common.sh` beside it). It must run as root (otherwise it says
  to pipe it into `sudo bash`) on Fedora: any other system is an error, and a
  Fedora other than 44 is a warning (the preflight decides). It installs
  `git-core` with dnf when git is missing, as on a stock Fedora 44 Server.
  It clones `https://github.com/GhostCode-IO/Evil_StepBROTHER.git` (branch
  `main`) into `/usr/local/src/ql810wc-print-proxy`, or updates the checkout
  already there with a fast-forward-only fetch and merge. It stops, and
  leaves the files and the branch in that directory as they were, when the
  directory is not owned by root, is neither empty nor a git checkout, is a
  checkout of another repository or cannot be fast-forwarded. Nothing else
  has changed by then, except that `git-core` may have been installed: the
  last two checks need git. Then it runs that checkout's setup.sh with the
  same options.
- **Checkout** (setup.sh in a clone of this repository, any clone). It
  installs `python3` and `iproute` when they are missing, writes
  `config.env` when there is none, then runs `./install.sh` with the
  arguments given after `--`.
- **Settings of an installed box are kept.** On a box that is installed
  already with a `config.env`, a checkout without one (another clone, or
  another `--dir`; and no `/etc/ql810wc-proxy/config.env`) makes setup.sh
  stop with exit status 3 before it writes a `config.env`: a new one from
  the template would replace the settings the box was installed with. The
  message names the `config.env` the installation uses, how to copy it into
  this checkout, and how to run setup.sh in the checkout you installed from.
  `--new-config` starts from new settings instead.
- **One run at a time.** setup.sh, `install.sh` and `uninstall.sh` hold a
  lock on `/run/ql810wc-proxy.lock` while they change the box; a second run
  stops with exit status 1 and says why. `--print-config` changes nothing
  and takes no lock.

| Option | Effect |
|---|---|
| `--clients "CIDR [CIDR...]"` | The networks of your Macs and iPhones: IPv4 or IPv6, space- or comma-separated, and the option may be repeated. A value that is not a network is a usage error; a network with host bits set, such as `192.0.2.5/24`, becomes `192.0.2.0/24` with a warning. Sets `SNMP_PRINTER_ALLOWED_CIDRS`, which also scopes Bonjour. Default for a new `config.env`: every IPv4 network of the interface that holds the default route (the one with the lowest metric). |
| `--name NAME` | `PRINTER_NAME_SUFFIX` (at most 45 bytes, without double quotes, backslashes, `$` or backticks): the printer appears as `Brother QL-810W [NAME]`. Default, and with `--name ""`: the box's host name. For a name that starts with `-`, write `--name=NAME`. |
| `--snmpv3` | Turns on SNMPv3 host monitoring: `SNMP_V3_ENABLE="yes"`, random passphrases of 32 letters and digits in `SNMP_V3_AUTH_PASS` and `SNMP_V3_PRIV_PASS` (later runs keep them), and `SNMP_V3_ALLOWED_CIDRS` (the monitoring system's sources) set to the client networks while it is empty, the template's example or the same as the client networks; a range of your own is kept. The passphrases are never printed or logged: read them with `sudo cat config.env` in the checkout. |
| `--new-config` | Write a new `config.env` from the template although the box is installed with settings from another `config.env` (see above). An existing `config.env` in this checkout is kept. |
| `--config-only` | Write or update `config.env`, print where it is, and stop before `install.sh`. The one-liner still clones or updates its checkout, and `git-core`, `python3` and `iproute` are still installed when they are missing. Those packages are recorded in the install manifest (`/var/lib/ql810wc-proxy/manifest`), which `uninstall.sh` reads. |
| `--print-config` | Print the `config.env` setup.sh would write, or the existing one. The SNMPv3 passphrases are masked, and so are the serial numbers (`USB_SERIAL`, `PERSONA_SERIAL`) and community names other than `public` and `internal` (only their first three characters are shown). Changes and installs nothing, so it stops with exit status 1 when `python3` or `iproute` is missing. In a checkout it needs no root, unless only root may read `config.env`. The one-liner uses the checkout in `--dir` as it is, without updating it, and stops with exit status 1 while there is none: use `--config-only` there. |
| `--dir DIR` | Bootstrap: clone into DIR instead of `/usr/local/src/ql810wc-print-proxy`. Ignored, with a warning, in a checkout. |
| `--ref REF` | Bootstrap: the branch or tag to use instead of `main`. Ignored, with a warning, in a checkout. |
| `-- ARGS` | Pass ARGS to `install.sh`, for example `-- --data-plane-only` (printing only). A usage error together with `--config-only` or `--print-config`, which do not run `install.sh`. |
| `-h`, `--help` | Show the help. |

`--config-only` and `--print-config` exclude each other. Every option that
takes a value also accepts the `--option=VALUE` form.

For forks and tests, the environment variables `QLP_REPO_URL`,
`QLP_SRC_DIR` and `QLP_REPO_REF` override the repository URL, the directory
and the ref (`--dir` and `--ref` take precedence). sudo drops other
environment variables, so set them after `sudo`, for example
`curl -fsSL <url> | sudo QLP_REPO_URL=<fork URL> bash`.

**`config.env`.** In checkout mode setup.sh writes `config.env` next to
`install.sh` (for the one-liner:
`/usr/local/src/ql810wc-print-proxy/config.env`), unless the installer
already reads another one (`/etc/ql810wc-proxy/config.env`, or the file
named by `QL_PROXY_CONFIG`): then it uses that. It is owned by root, mode
0600, and never readable by others, not even while it is written, so read
and edit it as root (`sudo cat config.env`, `sudo vi config.env`). It is a
copy of [`config.sample.env`](config.sample.env), under a header of its own,
with these values filled in; every other key keeps the template default
([Configuration](#configuration)):

- `SNMP_PRINTER_ALLOWED_CIDRS`: the value of `--clients`, or every IPv4
  network of the default-route interface. With no IPv4 default route or
  address, setup.sh stops and asks for `--clients`. When it detects the
  network and the box is also on other networks through an Ethernet or
  Wi-Fi interface, it warns that computers and phones there do not find the
  printer, and shows the `--clients` value that admits them too. Networks of
  bridges, containers and tunnels get a note only.
- `PRINTER_NAME_SUFFIX`: the value of `--name`, else empty (the host name).
- `SNMP_V3_ENABLE="no"`, or with `--snmpv3` the SNMPv3 values above.
- `PROXY_IP` stays empty: `install.sh` uses the default-route address.

An existing `config.env` is kept as it is. Only the keys of the options you
pass (`--clients`, `--name`, `--snmpv3`) are rewritten in place, and nothing
is detected. setup.sh lists each value it rewrites, old and new; a new
passphrase is named, never shown. While no name is set, setup.sh warns when
the host name is `localhost`, `localhost.localdomain` or `fedora`, because
the printer would appear as `Brother QL-810W [fedora]`: set one with
`sudo hostnamectl set-hostname NAME` or pass `--name`.

- Exit status: 0 installed: `install.sh` finished, and its selftest (which
  `-- --only N` and `-- --no-selftest` leave out) reached at least T0
  (printing); with `--config-only` or `--print-config`: done; 1 an error
  (the logs are in `/var/log/ql810wc-proxy/`), including another run that
  holds the lock; 3 a person must act first, for example connect the
  printer, switch it on, turn Editor Lite off or choose which settings to
  keep (the `HUMAN` lines say what, and setup.sh then names the command to
  run again); 64 bad usage.

On success setup.sh ends with a short "what now": the name P-touch Editor
(Mac) lists the printer under (iPrint&Label for iOS finds it too), the IP
address for P-touch's "Connect to printer with an IP address" (not tested
yet), where `config.env` is (with SNMPv3 on, also the SNMPv3 user, and that
its passphrases are in `config.env`), how to upgrade, and the `selftest.sh`
and `uninstall.sh` commands. With `ENABLE_MDNS="no"` it says that Bonjour is
off and gives only the IP-address route. When the options after `--` left
steps out (for example `--data-plane-only`), it names the parts that are not
installed yet instead.

### What setup.sh changes on the system

setup.sh itself adds the checkout, its `config.env` and, when they are
missing, the packages `git-core`, `python3` and `iproute`. It records those
packages in the install manifest (`/var/lib/ql810wc-proxy/manifest`), like
the installer's own. Everything else below is done by `install.sh` and its
steps and recorded in the same manifest. [`uninstall.sh`](#uninstall)
reverses it, except that it removes packages only with `--remove-packages`.
The checkout and its `config.env` stay.

- **Packages**, only the missing ones, without weak dependencies: `cups
  cups-client net-snmp net-snmp-utils avahi avahi-tools nss-mdns usbutils
  python3 python3-pyyaml python3-pyusb policycoreutils-python-utils`. With
  `INSTALL_BROTHER_QL="yes"` (the default) also `brother_ql_next` 0.12.0
  from PyPI in `/opt/ql810wc-proxy/venv`, for test prints only.
- **A system user `ql810wc`** (no login shell, no home), with the
  supplementary group `lp` for the printer's USB device. Both daemons run as
  this user.
- **Code and settings**: `/usr/local/libexec/ql810wc-proxy` (the daemons and
  `qlctl`) and `/etc/ql810wc-proxy` (`runtime.env`, and `identity.env` with
  the printer's UUID, kept across reinstalls).
- **Units**: `ql810wc-proxyd.socket` and `ql810wc-proxyd.service` (TCP 9100),
  `ql810wc-snmp-agent.service`; snmpd and avahi-daemon are enabled and
  started. A CUPS that step 10 installed is started through `cups.socket`.
- **Configuration of other services**: an snmpd drop-in
  (`/etc/snmp/snmpd.conf.d/ql810wc-proxy.conf`) plus an `includeDir` block in
  `/etc/snmp/snmpd.conf`; an Avahi service file; quirks that keep ipp-usb and
  CUPS's usb backend away from the printer; a udev rule; a raw, unshared
  CUPS queue `QL810Wc-proxy` for applications on the box itself. The SNMPv3
  user exists only with `--snmpv3`.
- **Firewall**: TCP 9100 is opened zone-wide, like a real printer. UDP 161
  (SNMP) and UDP 5353 (Bonjour) are opened only for the client ranges, as
  source-scoped rules. Rules go into the zone of the default-route
  interface. Cockpit (9090) and CUPS's own port (631) are never touched, nor
  any rule the installer did not add.
- **SELinux** stays enforcing. No policy module is installed.

### Where things live

| Path | What |
|---|---|
| `/usr/local/src/ql810wc-print-proxy` | the checkout the one-liner creates. Any other checkout works the same way: the commands below run from the one you installed from. |
| `<checkout>/config.env` | your settings (root, mode 0600: read and edit it with `sudo`) |
| `/usr/local/libexec/ql810wc-proxy` | the installed code |
| `/etc/ql810wc-proxy` | `runtime.env` (settings for the daemons, no secrets) and `identity.env` |
| `/var/lib/ql810wc-proxy` | the install manifest, pristine backups of every file the installer changed, and `config-path`: which `config.env` the last install used |
| `/var/log/ql810wc-proxy` | one log per install run, including the selftest's report |
| `/run/ql810wc-proxy/state.json` | what `ql810wc-proxyd` last read from the printer |
| `/run/ql810wc-proxy.lock` | the lock that keeps a second setup.sh, `install.sh` or `uninstall.sh` out while one runs |

The full list: [docs/architecture.md](docs/architecture.md#3-installed-layout).

## Upgrade

On a box installed with the one-liner, run it again without options: it
updates `/usr/local/src/ql810wc-print-proxy` (fast-forward only), keeps
`config.env` and redeploys what changed:

```bash
curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
```

If you installed with `--dir DIR`, pass the same `--dir DIR` again; if you
installed a branch or tag with `--ref REF`, pass the one to upgrade to. The
other options rewrite the settings they name, as on the first run:
`--clients` sets `SNMP_PRINTER_ALLOWED_CIDRS`, `--name` sets
`PRINTER_NAME_SUFFIX`, and `--snmpv3` sets `SNMP_V3_ENABLE="yes"` (it keeps
usable passphrases and a monitoring range of your own in
`SNMP_V3_ALLOWED_CIDRS`). A change you have made to one of those settings
in `config.env` is then replaced; setup.sh lists every value it rewrites.

A box installed from a checkout of your own is upgraded from that checkout.
The one-liner uses its own checkout in `/usr/local/src/ql810wc-print-proxy`;
on such a box it stops with exit status 3 before it writes a `config.env`
there (unless your settings are in `/etc/ql810wc-proxy/config.env`, which
every checkout without a `config.env` of its own uses). Its message names
the `config.env` the box was installed with, the command that copies it
into the new checkout, and the command that runs setup.sh in your checkout
instead; with `--new-config` it would start from new settings (the detected
network, the host name, SNMPv3 off). To upgrade your checkout:

```bash
cd Evil_StepBROTHER
git pull --ff-only
sudo diff config.sample.env config.env   # new settings? copy the ones you need into config.env (as root)
sudo ./setup.sh                          # keeps config.env, redeploys, runs the selftest
```

The one-liner's checkout belongs to root: to update it this way, use
`sudo git -C /usr/local/src/ql810wc-print-proxy pull --ff-only`.

A copy that is not a git checkout (an unpacked archive, for example) cannot
be updated with `git pull`. Move such an installation to the one-liner:
copy its `config.env` to `/etc/ql810wc-proxy/config.env`, then run the
one-liner. Its checkout has no `config.env` of its own, so setup.sh and the
installer use that file, and `uninstall.sh` leaves it in place:

```bash
sudo install -m 0600 -o root -g root <old copy>/config.env /etc/ql810wc-proxy/config.env
curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
```

The installer redeploys the code to `/usr/local/libexec/ql810wc-proxy`
(removing files the new version no longer ships) and regenerates
`/etc/ql810wc-proxy/runtime.env`. It restarts `ql810wc-proxyd` only when its
code or settings changed. `ql810wc-snmp-agent` is reloaded, with no SNMP
gap, when only the settings, the printer identity or the OID map changed,
and restarted only for new code or a new unit file. snmpd is restarted
(never reloaded) only when its configuration changed. The printer identity
(`PRINTER_UUID`) is kept, so P-touch keeps recognising the printer.

Upgrade between jobs. A stopping `ql810wc-proxyd` gives the label being
printed 10 seconds to finish. While `ql810wc-snmp-agent` restarts (about a
second) or snmpd restarts, a P-touch print that is polling the printer's
status fails.

To change a setting, edit `config.env` as root (for example
`sudo vi config.env`; it is root's, mode 0600) and run `sudo ./setup.sh` (or
`sudo ./install.sh`) again. The client ranges and the name can also be
changed with `sudo ./setup.sh --clients "CIDR [CIDR...]" --name NAME`. One
step alone: `sudo ./install.sh --only 20` (data plane), `--only 30` (SNMP),
`--only 40` (Bonjour). `--only` leaves out the selftest; run
`sudo ./selftest.sh` afterwards.

## Uninstall

From the checkout you installed from (for the one-liner:
`cd /usr/local/src/ql810wc-print-proxy`):

```bash
sudo ./uninstall.sh --dry-run          # show what would be done
sudo ./uninstall.sh                    # undo what install.sh recorded
sudo ./uninstall.sh --purge            # also delete logs, backups and the saved printer identity
sudo ./uninstall.sh --remove-packages  # also dnf-remove the packages setup.sh and install.sh installed
```

`uninstall.sh` replays the install manifest
(`/var/lib/ql810wc-proxy/manifest`), newest entry first:

- stops and disables the units it enabled, and deletes the files and
  directories it created, including the validation venv;
- deletes the CUPS queue it created, and removes only the firewall rules and
  SELinux labels the installer added;
- removes the SNMPv3 user it created (with snmpd stopped while net-snmp's
  persistent file is edited);
- restores every file the installer modified from its pristine backup, and
  removes the managed `includeDir` block from `/etc/snmp/snmpd.conf`;
- restarts (Avahi: reloads) only the services that are running and read
  something it changed. snmpd stays stopped if the installer had enabled it.

It keeps a directory that still holds files the installer did not create (a
Unix socket that nothing listens on any more, such as the AgentX socket
snmpd leaves in `/var/agentx` when it stops, is removed first), and keeps the
snmpd include block while other files in `/etc/snmp/snmpd.conf.d` still need
it; it says so in each case. While firewalld is stopped, its rules are
removed from the permanent configuration with `firewall-offline-cmd`. What
cannot be reversed right now (the CUPS scheduler down, firewalld down without
`firewall-offline-cmd`, a failing `userdel` or `systemctl disable`) stays in
the manifest: the run then ends with exit status 1 and "partially
uninstalled", and running it again once that is fixed finishes the job.
`--purge` keeps `/var/lib/ql810wc-proxy` while such records remain. Cockpit
(9090), CUPS itself (631), other queues and anything else the installer did
not create are never touched. Without `--purge`, `PRINTER_UUID` survives in
`/var/lib/ql810wc-proxy`, so a reinstall presents the same printer.

Packages stay installed unless you pass `--remove-packages`. It runs
`dnf -y remove` on every package the manifest lists: those step 10
installed, and `git-core`, `python3` and `iproute` when setup.sh installed
them. dnf also removes every package that depends on one of them, for
example `git` if you installed it later. `sudo ./uninstall.sh --dry-run`
lists the recorded packages first. `--remove-packages` and `--purge` also
work after a completed uninstall: `--remove-packages` then takes the
packages from the archived manifests
(`/var/lib/ql810wc-proxy/manifest.uninstalled-*`) and removes those still
installed, and `--purge` deletes the logs and `/var/lib/ql810wc-proxy` with
the saved printer identity.

The checkout and its `config.env` stay. To remove the one-liner's checkout
as well: `sudo rm -rf /usr/local/src/ql810wc-print-proxy`.

- Exit status: 0 done (or nothing to do); 1 an error, or partially
  uninstalled; 64 unknown option.

## How it works

```
 Client (P-touch Editor)          Fedora 44 box                                QL-810Wc
 ───────────────────────          ─────────────────────────────────────────    ────────
 TCP 9100  Brother raster ──────▶ ql810wc-proxyd ── /dev/usb/lpN (usblp) ────▶ prints
                                    └ polls the status while idle; writes state.json
 UDP 161   SNMP v1/v2c    ◀─────▶ snmpd ─ context "printer" ─ AgentX ─ ql810wc-snmp-agent
           SNMPv3 authPriv ◀────▶ snmpd ─ default context: this host's own MIBs
 UDP 5353  Bonjour        ◀────── avahi-daemon: "Brother QL-810W [<host>]", _pdl-datastream._tcp
 Local apps ─▶ CUPS raw queue ─ socket://127.0.0.1:9100 ─▶ ql810wc-proxyd
```

- The QL-810Wc speaks Brother raster, and two-colour data travels inside that
  stream. `ql810wc-proxyd` is the only process that opens the printer. It
  writes each job to USB unchanged and never re-renders it or synthesizes red;
  a job whose colour mode the loaded roll cannot print is refused instead,
  before any of its print data reaches the printer (`RAW_COLOR_CHECK`).
- What P-touch needs besides the data is on SNMP: above all the printer's
  32-byte status block, which carries the loaded roll.
  `ql810wc-snmp-agent` serves it live, together with the rest of the persona
  in `agent/oid-map.yaml`, in its own SNMP context, so the persona never mixes
  with the host's MIBs.
- Avahi publishes a Bonjour record `Brother QL-810W [<host>]` of type
  `_pdl-datastream._tcp` with `usb_MDL=QL-810W` and the printer's UUID, which
  is what P-touch Editor for Mac looks for.
- Design contract: [docs/architecture.md](docs/architecture.md). Evidence
  behind it: [dev/notes/](dev/notes/README.md).

## Requirements

- **Fedora 44 Server, x86_64 or aarch64.** The tested unit is a Raspberry
  Pi 4. setup.sh and the preflight warn on other Fedora releases and go on,
  and refuse other distributions. Fedora 45 (released in November 2026) and
  later releases have not been tested. Fedora's download page offers only the
  newest release; the Fedora 44 images are in Fedora's release directories
  ([docs/fedora-on-raspberry-pi.md](docs/fedora-on-raspberry-pi.md#1-download-the-image)).
  Fedora 44 gets updates until about a month after Fedora 46 is released.
  Any system locale works: the scripts parse tool output under `C.UTF-8`.
- Root (sudo), and network access to the Fedora repositories; the one-liner
  also needs GitHub. PyPI access is needed only for the optional validation
  tool.
- At least 300 MB free on `/` (the installer warns below that).
- firewalld active, as on Fedora Server, if the installer is to manage the
  firewall. Otherwise open the ports yourself (see
  [Security model](#security-model)).
- SELinux enforcing is supported and stays on. No custom policy module is
  needed.
- **Printer:** a Brother QL-810Wc on USB, powered on, with **Editor Lite off**
  (green LED dark). With Editor Lite on, the printer shows up as a USB drive
  (`04f9:209f`) instead of a printer (`04f9:209c`).
- **Clients:** P-touch Editor 5.3 for Mac (verified with 5.3.23) or Brother
  iPrint&Label for iOS, on a network listed in `SNMP_PRINTER_ALLOWED_CIDRS`
  (setup.sh sets it to the box's own network unless you pass
  `--clients`). P-touch Editor for Windows with Brother's driver and a raw
  TCP/IP port should work, but is untested.

## Manual install with install.sh

setup.sh writes `config.env` and runs `install.sh`. To do those steps by
hand:

```bash
sudo dnf -y install git-core
git clone https://github.com/GhostCode-IO/Evil_StepBROTHER.git && cd Evil_StepBROTHER
cp config.sample.env config.env && chmod 600 config.env
"${EDITOR:-vi}" config.env          # at least SNMP_PRINTER_ALLOWED_CIDRS (your client network)
sudo scripts/00-preflight.sh        # read-only report, changes nothing
sudo ./install.sh                   # steps 00 to 40, then selftest.sh
```

- `install.sh` runs `scripts/00-preflight.sh`, `10-deps.sh`,
  `20-cups-queue.sh` (data plane), `30-snmp-agent.sh` (SNMP) and
  `40-discovery.sh` (Bonjour) in order, then [`selftest.sh`](#selftest).
  Every step is idempotent: a re-run changes only what differs, and
  everything it changes is recorded for `uninstall.sh`.
- Options: `--data-plane-only` (steps 00 to 20: printing only, T0/T1),
  `--only N` (that step alone, without the selftest), `--from N`, `--skip N`
  (repeatable), `--no-selftest` (leave out the final selftest).
- Exit status: 0 installed, and the selftest reached at least T0 (printing);
  1 a step failed, or the steps succeeded but the selftest found T0 not
  reached, or another setup.sh, install.sh or uninstall.sh is changing the
  box at the same time; 3 a person must act first (the message says what,
  for example "connect the printer", "turn Editor Lite off" or "start CUPS",
  or how to keep the settings the box was installed with); 64 unknown
  option.
- **When the selftest finds T0 not reached, `install.sh` exits 1** with
  "installed, but selftest reported problems (see above)". Every step has
  succeeded by then. The cause is usually at the printer: unplugged or
  switched off (it switches itself off after about an hour without
  `ql810wc-proxyd`'s status polls), Editor Lite on, the cover open or no roll
  loaded. Fix what the `FAIL` lines say, then run `sudo ./selftest.sh`
  again. A failed higher tier (T1 to T3) never changes the exit status.
- The local CUPS queue (`CUPS_QUEUE`) needs the CUPS scheduler. A CUPS that
  step 10 installed has never run, so step 20 starts `cups.socket` for it. A
  CUPS that was there before and is stopped stays stopped: step 20 does
  everything else, then ends with exit status 3, and `install.sh` stops
  there (steps 30 and 40 have not run). Start CUPS (`sudo systemctl enable
  --now cups.socket`) or set `CUPS_QUEUE=""`, then run `sudo ./install.sh`
  again.
- Preflight stops the install with 3 while no printer is attached. To install
  anyway, add `--skip 00`: `ql810wc-proxyd` waits for the printer. The final
  selftest then fails T0 with "no printer on USB" (on a first install also
  with the missing CUPS usb exclusion: step 20 could not learn the printer's
  product ID), so `install.sh` exits 1. Once the printer is attached and
  switched on, run `sudo ./install.sh --only 20` so the CUPS USB exclusion
  learns its product ID, then `sudo ./selftest.sh`.
- Log: `/var/log/ql810wc-proxy/install-<UTC time>.log`, including the
  selftest's report.
- Configuration file: the file named by `QL_PROXY_CONFIG`
  (`sudo QL_PROXY_CONFIG=/path/to/config.env ./install.sh`: sudo drops other
  environment variables), else `./config.env` next to `install.sh`, else
  `/etc/ql810wc-proxy/config.env`. With none of them, a first install runs
  on the template defaults, with a warning. On a box that is installed
  already with a `config.env`, a run that finds none stops with exit status
  3 instead of replacing those settings with the template defaults: its
  message names the `config.env` the box was installed with and how to
  copy it, or which checkout to run from.

Then set up the clients: [docs/client-setup.md](docs/client-setup.md).
Keep the checkout: upgrades, `selftest.sh` and `uninstall.sh` run from it.

## Selftest

`selftest.sh` checks the installed system end to end, the way clients use
it, and reports the highest tier it reaches. `install.sh` (and so setup.sh)
runs it at the end; run it yourself from the checkout whenever you want to
know where things stand:

```bash
sudo ./selftest.sh                          # every check, then the verdict
sudo ./selftest.sh --print-test             # also print a black/red test label and confirm it by eye
sudo ./selftest.sh --print-test --mono      # a black-only test label (single-colour continuous rolls)
sudo ./selftest.sh --quiet                  # only the WARN and FAIL lines, and the verdict
sudo ./selftest.sh --json > ~/selftest.json # summary as JSON on stdout, the report on stderr
```

It changes nothing on the system; `--print-test` prints one label. Each check
prints one line starting with `PASS`, `WARN`, `FAIL`, `SKIP` or `INFO`, and
every `FAIL` says what to do. `WARN` lines point out risks but never lower the
tier. `--no-color` (or the `NO_COLOR` variable) turns colours off. Run it as
root: as another user it does not check the firewall rules or the SELinux
audit log, nor SNMPv3 and the allowed client ranges while `config.env` is
readable by root only, and it says so.

| Tier | Reached when |
|---|---|
| T0 | `ql810wc-proxyd` listens on TCP `RAW_PORT` and keeps reading a current status block from the printer: printer mode (not Editor Lite), no error, a roll loaded. Also: its idle CPU use stays low, ipp-usb and CUPS's usb backend are kept away from the printer, and the firewall admits `RAW_PORT`. |
| T1 | T0, and a black/red roll (DK-22251) is loaded, so the printer takes two-colour jobs. "Confirmed" once a `--print-test` label was checked by eye. |
| T2 | T1, and discovery works: snmpd and `ql810wc-snmp-agent` run, every community answers with `MDL:QL-810W` and sees none of the host's data, an unknown community gets no answer, snmpd listens on the UDP 161 wildcard, the Bonjour record resolves with `usb_MDL=QL-810W` and the printer's UUID, `SNMP_PRINTER_ALLOWED_CIDRS` and the Bonjour range hold real client networks (`t2.reach.snmp`, `t2.reach.mdns`: not empty, not only loopback or documentation ranges such as the template's 192.0.2.0/24), and the firewall admits UDP 161 and 5353 from them. |
| T3 | T2, and P-touch sees the installed roll: the status block read over SNMPv1 with the community `internal` is the printer's own, byte for byte, and fits the shipped OID map. |
| host SNMP (bonus) | The SNMPv3 user reads this host's own MIBs in the default context. Shown as ` (+host SNMP)` after the tier; skipped while SNMPv3 is off (setup.sh's default; `--snmpv3` turns it on). |

The report ends with one line per tier and the verdict, for example on a box
with DK-22251 loaded and SNMPv3 off:

```
== verdict ==
T0 data plane      reached
T1 two-colour      capable (not printed)
T2 discovery       reached
T3 live media      reached
host SNMP (bonus)  off
NOTE: identity strings in oid-map.yaml (sysDescr, sysObjectID, ...) are not yet verified on a real QL-810Wc; P-touch Editor for Mac and iPrint&Label for iOS work with them as they are
TIER REACHED: T3
```

- **The tiers build on each other.** A tier counts only when every tier below
  it is reached. With a die-cut or single-colour roll loaded, T1 cannot be
  verified, so the verdict is `TIER REACHED: T0`, and a `NOTE` line names the
  higher tiers that passed anyway. Load DK-22251 to check T1.
- **`NOTE: identity strings in oid-map.yaml …`** comes with every T3 pass
  while no firmware version is validated: `validated_firmware` in
  `agent/oid-map.yaml` is empty, and `ql810wc-proxyd` cannot read the
  printer's firmware version over USB. It is information, not a problem and
  not a degraded result: the identity strings are placeholders (see
  [What works](#what-works)), and the status and roll that P-touch reads
  were verified live.
- **`DEGRADED: T0-T2 with manual media`** means that this printer's status
  block does not fit the shipped OID map (another model code, a media type
  encoding P-touch does not read, or an unexpected header). Printing and
  discovery still work; select the roll by hand in P-touch. The verdict is
  then T2 at most.
- **`--print-test`** sends a test label to TCP `RAW_PORT` with
  `qlctl test-print` and asks on the terminal whether it came out right. "Yes"
  makes T1 "confirmed"; "no" fails T1, or T0 with `--mono`. Without a terminal,
  or without an answer within 10 minutes, the label counts as printed but not
  confirmed. It refuses what the printer would reject: a black/red label on a
  single-colour roll, `--mono` on DK-22251 (that error needs a power cycle),
  and any test label on die-cut labels.
- **`--json`** writes one document to standard output: `tier_reached`,
  `verdict`, `exit_code`, `host_snmp`, `degraded`, `firmware_validated`, the
  status and problems of each tier, the `notes` (such as the firmware
  `NOTE`), and every check. The report goes to standard error. The printer's
  serial number is masked in both. Redirect the document to a file outside
  the one-liner's checkout, which only root may write to.
- Exit status: 0 T0 reached, whatever the higher tiers say; 1 T0 not
  reached; 64 usage error. If the selftest breaks off before its verdict, it
  prints `TIER REACHED: NONE` and exits with a non-zero status.
- Right after `ql810wc-proxyd` (re)starts, as at the end of an install, it
  may not have found the printer yet; the selftest waits up to 15 seconds for
  it before judging T0. After `--data-plane-only`, T2 and T3 are reported as
  not reached, since nothing was set up for them.
- The client ranges are judged only when `config.env` is readable (as root).
  Warnings that never lower a tier: `t2.reach.mdns.local` (no Bonjour range is
  a subnet of this host, and mDNS queries never cross a router),
  `t2.listen.ipv6` (IPv6 client ranges, but snmpd does not listen on IPv6),
  `host.reach` (`SNMP_V3_ALLOWED_CIDRS` holds only documentation ranges), and
  `t2.agent.canreload` / `t2.agent.reload` (the agent's unit file is outdated,
  or the agent rejected its last reload).

Problems the report points to are explained in
[docs/troubleshooting.md](docs/troubleshooting.md#reading-the-selftest-report).

## qlctl

`qlctl` is installed in `/usr/local/libexec/ql810wc-proxy/`. Run it as
`python3 -I -B /usr/local/libexec/ql810wc-proxy/qlctl <command>`:

| Command | What it does |
|---|---|
| `status [--json [--show-serial]]` | What `ql810wc-proxyd` last saw: printer state, loaded roll, errors, last job. |
| `test-print [--two-color \| --mono] [--pattern bars\|black] [--width MM]` | Prints a test label through TCP 9100 (default: two-colour bars on 62 mm). Refuses a job that does not suit the loaded roll, for example `--mono` on DK-22251 or any job on die-cut labels, and a `--width` other than the loaded roll's: the job's `ESC i z` would make the printer report "replace media". `--force` sends it anyway; with `RAW_COLOR_CHECK` on, `ql810wc-proxyd` still refuses a label whose colour mode the loaded roll cannot print (`--mono` on DK-22251, a two-colour label on a single-colour roll), and qlctl says so and exits with 3. |
| `probe [--json [--show-serial]]` | Reads the status straight from USB (needs root). Refused while `ql810wc-proxyd` runs. |
| `version` | Prints the version. |

`--json` masks the printer's USB serial (its first three characters remain,
as in the logs); add `--show-serial` for the full serial. Exit status: 0 ok,
1 failure, 2 usage error, 3 refused (including a roll that does not suit
the test label, or a test label that `ql810wc-proxyd` refused) or printer
not ready.

## Configuration

All site-specific values live in `config.env` (gitignored), a copy of the
committed template [`config.sample.env`](config.sample.env). setup.sh writes
it on its first run ([setup.sh reference](#setupsh-reference)): it fills in
`SNMP_PRINTER_ALLOWED_CIDRS`, `PRINTER_NAME_SUFFIX` and `SNMP_V3_ENABLE` (with
`--snmpv3` also the SNMPv3 passphrases and `SNMP_V3_ALLOWED_CIDRS`) and leaves
every other key at the default below. setup.sh makes the file root's, mode
0600: edit it with `sudo` (for example `sudo vi config.env`), then run
`sudo ./setup.sh` again. The installer parses the file and never sources it:

- `KEY="value"` lines only, so shell syntax does not work.
- Values cannot contain double quotes, `$(` or backticks. Settings handed to
  the daemons (everything except the communities and the SNMPv3 credentials)
  also refuse backslashes and `$`.
- Yes/no settings accept yes/no, true/false, on/off and 1/0.
- Unknown keys are reported and ignored. A key missing from `config.env` takes
  the default below.

Nothing about the printer's identity is hard-coded: its USB product ID and
serial are detected at runtime.

| Key | Default | Meaning |
|---|---|---|
| `PROXY_IP` | empty | IPv4 address clients use to reach the box. Empty: the address of the default-route interface, detected at install time. The firewalld zone of that interface receives all rules, and the persona's MAC address is that interface's unless `PERSONA_MAC` is set. |
| `RAW_PORT` | `9100` | TCP port of the raw print listener. P-touch Editor for Mac always prints to 9100. |
| `RAW_BIND` | empty | Listen address. Empty: every IPv4 and IPv6 address (dual-stack), like a real printer. One address of this host: listen only there; the local CUPS queue then connects to that address. |
| `RAW_ALLOWED_CIDRS` | empty | Empty: `RAW_PORT` is open to the whole firewalld zone. Otherwise only these sources (space-separated IPv4/IPv6 CIDRs). P-touch for Mac may connect over IPv6, so list your IPv6 client ranges too. |
| `RAW_BIDIRECTIONAL` | `no` | `yes` relays bytes from the printer back to the TCP client. Real networked QLs never send anything on 9100. |
| `STATUS_POLL_SECONDS` | `3` | Seconds between status polls of an idle printer (1 to 60). The printer is never polled while a job prints. The persona's staleness limit follows it (see below the table). |
| `RAW_IDLE_TIMEOUT` | `300` | Seconds a connected client may send nothing before the proxy closes the connection (10 to 86400). An unfinished job is then cancelled and the printer resynchronised; after a finished job it is an ordinary close (P-touch for Mac keeps the connection open for a while after a job). |
| `MAX_JOB_BYTES` | `16777216` | Largest job accepted on one connection, in bytes (65536 to 1073741824). A larger job is cancelled and the printer resynchronised. 16 MiB holds an uncompressed two-colour label 1 m long. |
| `RAW_COLOR_CHECK` | `yes` | Refuse a job whose colour mode the loaded roll cannot print: a black-only job while DK-22251 is loaded, or a black/red job on a single-colour roll. The printer rejects such a job itself, and after a black-only job on DK-22251 the tested unit needed a power cycle. None of a refused job's print data reaches the printer: the client's connection is reset (macOS may still show the job as completed), and `qlctl status` shows the job as `refused`, with the reason. A job whose colour mode or roll is not certain goes through. `no`: forward every job unchecked. |
| `CUPS_QUEUE` | `QL810Wc-proxy` | Raw CUPS queue for applications on the box. Its device is the proxy (`socket://127.0.0.1:RAW_PORT`), so CUPS never opens the USB printer. Empty: no queue. An existing queue of that name that the installer did not create is left alone. |
| `CUPS_QUEUE_SHARED` | `no` | Share that queue over CUPS/IPP. Keep `no`: a shared raw queue attracts PDF jobs the QL cannot print. |
| `USB_VENDOR_ID` | `04f9` | Brother's USB vendor ID. |
| `USB_PRODUCT_ID` | empty | Empty: any product named `QL-…` (the QL-810W and QL-810Wc report `209c`). Set it to bind one product ID. |
| `USB_SERIAL` | empty | Empty: the first matching printer. The printer reports a different USB serial with Editor Lite on, so pin the printer-mode one. |
| `IPP_USB_EXCLUDE` | `yes` | Add an ipp-usb quirk that keeps ipp-usb away from the QL (and from nothing else). ipp-usb's IPP-over-USB mode disconnects the raw printer interface the proxy needs. |
| `MEDIA_DEFAULT` | `62red` | brother_ql label id of the primary roll (`62red` = DK-22251). Validated, but not used yet: it is meant for a manual-media fallback ([Limitations](#limitations)). |
| `MEDIA_SECOND` | empty | brother_ql label id of a second roll, such as `29x90` for DK-11201. Validated, but not used yet. |
| `INSTALL_BROTHER_QL` | `yes` | Install `brother_ql_next` 0.12.0 into `/opt/ql810wc-proxy/venv` for test prints. Needs PyPI. A failure does not stop the install. `no` removes the venv the installer created. |
| `SNMP_PRINTER_COMMUNITIES` | `public internal` | Read-only v1/v2c communities that see the printer persona, and nothing else. P-touch Editor for Mac uses `internal`; Windows tools and BRAdmin use `public`. |
| `SNMP_PRINTER_COMMUNITY` | empty | Deprecated single-community form; added to the list above. |
| `SNMP_PRINTER_ALLOWED_CIDRS` | `192.0.2.0/24` | **Replace this placeholder** (setup.sh does: `--clients`, or the box's own IPv4 network). Sources (IPv4/IPv6 CIDRs) allowed to query the persona on UDP 161, enforced by snmpd and by the firewall. This host (127.0.0.1) is always allowed. Also the default for `MDNS_ALLOWED_CIDRS`. Empty: only this host. While it holds no real client network, the installer warns and the selftest does not report T2. |
| `SNMP_AGENT_MODE` | `agentx` | `agentx`: the full persona in SNMP context `printer` (recommended). `pass_persist`: degraded fallback that serves only the Brother and Printer-MIB subtrees; enough for P-touch for Mac, but discovery by Windows tools and mobile apps is expected to fail. |
| `SNMP_V3_ENABLE` | `auto` | SNMPv3 access to the host's MIBs. `auto`: on once both passphrases below are set; `yes`: must be configured; `no`: off, and the user the installer created is removed. setup.sh writes `no`, or `yes` with `--snmpv3`. |
| `SNMP_V3_USER` | `nms` | SNMPv3 user name (1 to 32 letters, digits, `.`, `_`, `-`). |
| `SNMP_V3_AUTH_PROTO` | `SHA-256` | `SHA`, `SHA-224`, `SHA-256`, `SHA-384` or `SHA-512`. |
| `SNMP_V3_AUTH_PASS` | `CHANGE-ME-auth-passphrase` | Authentication passphrase, at least 8 characters. Secret. |
| `SNMP_V3_PRIV_PROTO` | `AES` | `AES` (AES-128). AES-192/256 are accepted with a warning; DES is not available on Fedora. |
| `SNMP_V3_PRIV_PASS` | `CHANGE-ME-priv-passphrase` | Privacy passphrase, at least 8 characters. Secret. |
| `SNMP_V3_ALLOWED_CIDRS` | `192.0.2.50/32` | **Replace this placeholder** (setup.sh `--snmpv3` sets the client ranges). Sources of your monitoring system. net-snmp cannot limit SNMPv3 users by source, so only the firewall enforces this. |
| `PRINTER_NAME_SUFFIX` | empty | Bonjour name suffix: `Brother QL-810W [<suffix>]` (setup.sh's `--name`). Empty: this host's short name. Longer than 45 bytes: shortened. The `Brother QL-810W` part is fixed, because P-touch for Mac derives the model from it. |
| `PRINTER_DISPLAY_NAME` | empty | Deprecated and ignored. |
| `PERSONA_LOCATION` | empty | SNMP `sysLocation` and the Bonjour `note`. Anyone on the network can read it, so avoid personal data. |
| `PERSONA_CONTACT` | empty | SNMP `sysContact`. Readable in cleartext by SNMP clients; prefer a role over a name. |
| `PERSONA_SERIAL` | empty | Serial number the persona reports over SNMP. Empty: the printer's own USB serial, as the real printer reports it. Readable in cleartext by SNMP clients. |
| `PERSONA_MAC` | empty | MAC address shown as the printer's (`ifPhysAddress.1` and the node name), six colon-separated hex pairs. Empty: the MAC of `PROXY_IP`'s interface. |
| `PERSONA_SYSNAME` | empty | SNMP `sysName` of the persona. Empty: `BRW` + the persona MAC as 12 hex digits, like a real Brother Wi-Fi printer. |
| `LOG_LEVEL` | `INFO` | Journal log level of `ql810wc-proxyd` and `ql810wc-snmp-agent`: `DEBUG`, `INFO`, `WARNING` or `ERROR` (any case; empty = `INFO`). |
| `ENABLE_MDNS` | `yes` | Publish the Bonjour record and open UDP 5353 to `MDNS_ALLOWED_CIDRS`. `no` removes both; Mac users then connect by IP address. |
| `MDNS_ALLOWED_CIDRS` | empty | Sources allowed to send mDNS queries (UDP 5353). Empty: `SNMP_PRINTER_ALLOWED_CIDRS`. IPv6 mDNS queries come from link-local addresses: add `fe80::/10` for IPv6-only clients. |

One daemon setting is derived rather than set: the installer writes
`STATUS_MAX_AGE_SECONDS`, 60 seconds or twice `STATUS_POLL_SECONDS` when that
is more, into `runtime.env`; it is no `config.env` setting. An idle status
older than that counts as stale, and the persona then reports the printer as
switched off. That is why every `STATUS_POLL_SECONDS` from 1 to 60 works.
The colour check (`RAW_COLOR_CHECK`) asks the printer for a new status when
the last one is older than `STATUS_MAX_AGE_SECONDS` (not while the printer's
input state is unknown, for example after a torn job), and lets the job
through unchecked if none comes.
Every daemon setting is described in `agent/ql810wc/config.py`.

## Security model

Details: [docs/security.md](docs/security.md).

| Port | Opened to | Served by | Used for |
|---|---|---|---|
| TCP 9100 (`RAW_PORT`) | the whole firewalld zone (default), or only `RAW_ALLOWED_CIDRS` | `ql810wc-proxyd` | print jobs, without authentication, like a real printer |
| UDP 161 | `SNMP_PRINTER_ALLOWED_CIDRS`, plus `SNMP_V3_ALLOWED_CIDRS` while SNMPv3 is on (source-scoped rich rules) | snmpd | printer persona (v1/v2c), host MIBs (v3) |
| UDP 5353 | `MDNS_ALLOWED_CIDRS` (default: `SNMP_PRINTER_ALLOWED_CIDRS`), source-scoped | avahi-daemon | Bonjour discovery |

- **UDP 5353 is opened only for the client ranges.** P-touch Editor for Mac
  discovers printers only through Bonjour, so Avahi must receive their
  queries. The zone-wide `mdns` service is never opened.
- Rules go into the zone of `PROXY_IP`'s interface, at runtime and
  permanently (no `--reload`). They are recorded and removed by
  `uninstall.sh`. Existing rules, Cockpit (9090) and CUPS (631) are never
  changed.
- The communities `public` and `internal` are read-only and mapped to the SNMP
  context `printer`, which holds nothing but the persona. `internal` is
  accepted because P-touch Editor for Mac uses no other community. No
  community can write.
- SNMPv3 authPriv (USM; SHA-256 and AES-128 by default) is the only way to
  the host's own MIBs. It can also read the persona with `-n printer`.
- The SNMPv3 passphrases are the only secrets. They stay in `config.env`
  (mode 0600: setup.sh creates it so, and the installer warns otherwise) and
  never reach `runtime.env`, a command line or a log.
- SELinux stays enforcing. The daemons run as the unprivileged system user
  `ql810wc` with systemd hardening.
- The one-liner runs a script from GitHub as root. To read it first, see
  [docs/security.md](docs/security.md#installing-with-setupsh).

## Limitations

- **CUPS cannot own the printer.** A fallback in which CUPS's own usb
  backend drives the printer, with manual media, is not implemented.
  `ql810wc-proxyd` always owns the USB printer and the installer blacklists
  the QL in CUPS's usb backend, so a `usb://` CUPS queue for the QL does not
  work while the proxy is installed. `MEDIA_DEFAULT` and `MEDIA_SECOND`
  belong to that fallback and are not used: without live status, the persona
  reports the printer as switched off.
- **Manual media when degraded.** If `selftest.sh` reports
  `DEGRADED: T0-T2 with manual media`, the printer's status block does not
  fit the shipped OID map (another model, or a firmware that encodes media
  differently), so P-touch may not recognise the roll. Printing and discovery
  still work; select the roll by hand in P-touch.
- **The ipp-usb exclusion disables driverless CUPS queues for the QL.** With
  `IPP_USB_EXCLUDE="yes"` (needed, see the table), a CUPS queue that reached
  the QL through ipp-usb (an `ipp://localhost:60000/…` URI) stops working. The
  installer reports such queues and leaves them in place; print through TCP
  9100 or `CUPS_QUEUE` instead.
- **Auto power-off.** The QL-810Wc switches itself off about 60 minutes after
  its last status request. `ql810wc-proxyd` polls an idle printer every
  `STATUS_POLL_SECONDS` (3 s), which keeps it on: the tested unit stayed on
  for almost four hours without a print job. While `ql810wc-proxyd` is
  stopped, nothing polls, so the timer runs again. While the printer is off,
  P-touch reports it as switched off.
- **DK-22251 needs two-colour jobs, even for black-only labels.** The
  printer rejects a mono job on this roll, and after one the tested unit
  needed a power cycle. `ql810wc-proxyd` cannot turn a mono job into a
  two-colour one (it forwards jobs unchanged), so it refuses it before its
  print data reaches the printer (`RAW_COLOR_CHECK`): nothing prints, the
  client's connection is reset, and `qlctl status` shows the job as
  `refused`. The application may not say so: macOS can show such a job as
  completed. Brother's printer drivers print in monochrome unless told
  otherwise: while this roll is loaded, set **Color** to
  **2-Color(Black/Red)** (macOS) or turn on 2-Color (Windows). See
  [client setup](docs/client-setup.md#media-rules).
- **One network segment for discovery.** Bonjour does not cross routers.
  Clients on another subnet do not find the box by themselves; P-touch for
  Mac can connect by IP address instead (untested).
- One job at a time. Further connections wait in the TCP backlog, as on the
  real printer.
- No IPP or AirPrint: only `_pdl-datastream._tcp` is advertised, and the local
  CUPS queue is raw (it accepts only data that already is Brother raster) and
  not shared.
- The local CUPS queue uses `printer-error-policy=retry-job`: if a job fails
  after the printer already took the data, CUPS retries it and the label can
  print twice.
- The identity strings are unverified placeholders (see
  [What works](#what-works)).

## Troubleshooting

Start with `sudo ./selftest.sh` in the checkout.
[docs/troubleshooting.md](docs/troubleshooting.md) explains its report and
covers setup.sh's messages, Editor Lite, ipp-usb, CUPS's usb backend,
"printer switched off", high CPU, SNMP, Bonjour, firewall, logs, SELinux, and
uninstall/reinstall.

## Repository layout

```
setup.sh                                one-step setup: clone (bootstrap), write config.env, run install.sh
install.sh  uninstall.sh  selftest.sh   entry points (run as root): install/upgrade, uninstall, end-to-end check
config.sample.env                       configuration template (setup.sh copies it to config.env)
scripts/NN-*.sh, scripts/lib/common.sh  ordered, idempotent install steps and their shared library
agent/ql810wc/                          Python package: data plane, SNMP persona, AgentX, USB, config
agent/ql810wc-proxyd, agent/qlctl       data-plane daemon and operator CLI
agent/brother_snmp_agent.py             SNMP persona (AgentX subagent, or pass_persist fallback)
agent/oid-map.yaml                      the shipped OID map
agent/{systemd,snmpd,avahi,ipp-usb,udev}/   unit files and configuration templates
tests/                                  stdlib unittest suite (with a fake printer)
docs/                                   client setup, Fedora on a Raspberry Pi, troubleshooting, security, architecture
dev/                                    for contributors only: SNMP walk tools, hooks, notes
```

## Development

- `dev/` is for contributors; installing and using the proxy never needs it.
  [dev/notes/](dev/notes/README.md) holds the hardware facts and the research
  summary. [dev/snmpwalk/](dev/snmpwalk/README.md) holds tools for
  contributors who can put a QL-810W(c) on Wi-Fi: read it with SNMP in known
  roll states, diff the readings and scrub them, to confirm or correct
  `agent/oid-map.yaml`.
- Tests: `python3 -m unittest discover -s tests -t .` (standard library
  only). The shell tests need bash 4.2 or later; on macOS, whose bash is 3.2,
  point `QLP_TEST_BASH` at a newer bash. Tests that need Linux (a real snmpd
  with VACM, bash parity checks) skip on macOS and run in CI in a Fedora 44
  container. `tests/test_docs.py` keeps the documentation in step with
  `setup.sh`, `install.sh`, `selftest.sh` and `uninstall.sh` (the options of
  the documented commands, the options and exit codes the README and
  `setup.sh --help` list), the report lines quoted from `selftest.sh`, the
  configuration table and every relative link. `tests/test_setup.py` runs
  setup.sh in both modes, with stub commands, a local git repository and
  setup.sh's test-only hooks in place of root, Fedora and the real
  installer.
- CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs three
  jobs on pushes to `main` and on pull requests: the hygiene check over every
  tracked file; `shellcheck -x` and the tests in a Fedora 44 container; and
  setup.sh in a fresh Fedora 44 container without git. That last job pipes
  setup.sh into bash as the one-liner does, with `--config-only` (the
  container has no systemd, SELinux or printer), checks the `config.env` it
  wrote and the `git-core` it installed, runs it again after a new commit to
  check the fast-forward update and that `config.env` stays as it was, and
  then runs `install.sh --only 10` and checks its packages, the validation
  venv and the recorded `config.env` path.
- Shell scripts must pass `shellcheck -x` (see `.shellcheckrc`).
- Secret hygiene: git history is public and permanent. Enable the guard once
  per clone:

  ```bash
  git config core.hooksPath dev/hooks
  ```

  It blocks raw SNMP walks, real configuration, keys, binary files, private
  IPv4 addresses and MAC addresses; CI runs it on every tracked file. Use
  documentation-only values in examples (`192.0.2.x`, `2001:db8::`,
  `00:00:5E:00:53:xx`). See
  [docs/security.md](docs/security.md#repository-hygiene).

## License

See [LICENSE](LICENSE).
