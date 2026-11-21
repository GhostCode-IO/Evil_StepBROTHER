# Fedora 44 Server on a Raspberry Pi 4

This guide takes a new Raspberry Pi 4 (called the box below) to a Fedora 44
Server you can reach over SSH, ready for this project's one-line setup.
Fedora's own documentation is the authority on every step; the links are at
the end. Any other 64-bit machine that runs Fedora 44 Server (x86_64 or
aarch64) works as well: install Fedora 44 Server on it as Fedora's
documentation describes, and continue with [step 5](#5-run-the-setup). The
x86_64 installer images are in
<https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/x86_64/iso/>.

## What you need

- A Raspberry Pi 4 Model B. Fedora also supports the Pi 400 and the Compute
  Module 4, and other boards; see Fedora's list before you buy something
  else.
- A good USB-C power supply, such as the official Raspberry Pi one. Fedora's
  wiki names a weak power supply as the most common problem.
- A microSD card of 16 GB or more, and a card reader for your computer.
- An Ethernet cable to your network. Fedora's wiki says Wi-Fi works on the
  Pi 4 Model B too (see step 4).
- For the first boot: a screen with a micro-HDMI cable and a USB keyboard.

## 1. Download the image

For ARM boards Fedora publishes a Server raw image for aarch64, prepared for
the Raspberry Pi. For Fedora 44 it is
`Fedora-Server-Host-Generic-44-1.7.aarch64.raw.xz` (about 1.2 GB), with the
checksum file `Fedora-Server-44-1.7-aarch64-CHECKSUM` beside it:

<https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/aarch64/images/>

Check the download as Fedora describes at <https://fedoraproject.org/security>:
fetch Fedora's OpenPGP key, verify the checksum file with `gpgv`, then check
the image with `sha256sum`.

Use Fedora 44. setup.sh and the installer warn on other Fedora releases
(they may work) and refuse other distributions. Fedora 45 (released in
November 2026) and later releases have not been tested with this project.
Fedora's download page offers only the newest release, so take the Fedora 44
image from the release directory above. Fedora maintains each release until
about four weeks after the release two versions later: Fedora 44 gets
updates until about a month after Fedora 46 is out. After that, its images
move to Fedora's archive,
<https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/44/>.

## 2. Write the image to the microSD card

Writing the image erases everything on the card.

**On a Fedora computer**, use Fedora's `arm-image-installer`. `lsblk` shows
which device the card is (for example `/dev/sdX` or `/dev/mmcblk0`):

```bash
sudo dnf install -y arm-image-installer
sudo arm-image-installer --image=Fedora-Server-Host-Generic-44-1.7.aarch64.raw.xz \
  --target=rpi4 --media=/dev/sdX
```

`--addkey=<file>` also installs an SSH public key for root, for example your
`~/.ssh/id_ed25519.pub`. Fedora's documentation describes this and the
tool's other options.

**On macOS or Windows**, write the image with Raspberry Pi Imager
(<https://www.raspberrypi.com/software/>): pick the custom image option at
the bottom of its operating system list and select the downloaded `.raw.xz`
file. Leave Imager's OS customisation settings (user, password, SSH, Wi-Fi)
empty: they are meant for Raspberry Pi OS, and Fedora does not apply them.

## 3. First boot

Put the card into the Pi and connect the Ethernet cable, the screen and the
keyboard, then the power supply. On the first boot Fedora's initial setup
runs on the screen. Create your user account there and give it administrator
rights, so that it can use `sudo`. Then log in on the screen with that
account.

The root file system of the image is small, and that is enough for this
project: the tested unit installed with less than 1 GB free on `/`, and the
installer warns only when less than 300 MB are free. Fedora's documentation
explains how to grow the file system if you want the rest of the card.

## 4. Host name, address and SSH

Give the box a host name. The printer appears under it, as
`Brother QL-810W [<host name>]`, and the setup warns while the name is still
`localhost`, `localhost.localdomain` or `fedora`:

```bash
sudo hostnamectl set-hostname labelprinter
```

Find the box's address: `ip -4 addr` on the screen, or the list of DHCP
clients in your router. Fedora Server runs an SSH server and its firewall
admits SSH, so from now on you can work from your computer:

```bash
ssh <user>@<address>
```

Bring the system up to date before you go on (recommended):

```bash
sudo dnf -y upgrade --refresh
sudo reboot
```

Wired Ethernet is the simpler choice for a print server. For Wi-Fi, which
is set up with NetworkManager's `nmcli` on a Server image, see Fedora's
documentation.

## 5. Run the setup

Connect the QL-810Wc to the Pi by USB and switch it on, with Editor Lite off
(green LED dark: press and hold the Editor Lite button until the LED goes
out). Then, on the box:

```bash
curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
```

What happens next, the options and what to do when it stops:
[README: Quick start](../README.md#quick-start) and
[setup.sh reference](../README.md#setupsh-reference).

## Fedora's documentation

- Fedora on Raspberry Pi: <https://docs.fedoraproject.org/en-US/quick-docs/raspberry-pi/>
- Fedora wiki, Raspberry Pi (supported models, `arm-image-installer`
  options, serial console):
  <https://www.fedoraproject.org/wiki/Architectures/ARM/Raspberry_Pi>
- Fedora Server documentation: <https://docs.fedoraproject.org/en-US/fedora-server/>
- Fedora 44 Server images: aarch64
  <https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/aarch64/images/>,
  x86_64
  <https://download.fedoraproject.org/pub/fedora/linux/releases/44/Server/x86_64/iso/>
  (after Fedora 44's end of life:
  <https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/44/>)
- Fedora Server downloads, the current release only (Fedora 45 or later, not
  tested with this project): <https://fedoraproject.org/server/download>
