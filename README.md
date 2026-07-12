# Dropzone

A private, self-hosted share point for your home network. Drop files or text on one
device and pick them up on another — no cloud, no accounts, everything stays on your LAN.

## Features

- **File & text sharing** with drag-and-drop upload and per-item share links
- **Live device detection** and an activity feed of who shared what
- **Chat** between devices on the network
- **Pinned items**, bulk delete, and zip download
- **Send-to-device** targeting so an item shows up only for the intended recipient
- **Burn-after-read notes** and per-item expiry
- **Server-side URL fetch** — paste a link and Dropzone pulls the file for you
- **Storage stats** and device renaming
- **Instant push updates** via SSE (with polling fallback)
- **Remote screen** viewing through a bundled noVNC client (x11vnc + websockify bridge)

## Requirements

- Python 3.8+
- Dependencies listed in `requirements.txt` (Flask, qrcode, websockify)

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open `http://<your-machine-ip>:8000` from any device on the network. Scan the
QR code at `/qr.svg` to open it on a phone.

## Configuration

All settings are optional environment variables:

| Variable               | Default    | Description                             |
| ---------------------- | ---------- | --------------------------------------- |
| `DROPZONE_PORT`        | `8000`     | Port to listen on                       |
| `DROPZONE_DATA`        | `./data`   | Data directory (files + SQLite DB)      |
| `DROPZONE_PIN`         | *(none)*   | Optional shared PIN; unset = open       |
| `DROPZONE_MAX_MB`      | `2048`     | Max upload size in MB                   |
| `DROPZONE_EXPIRE_DAYS` | `0`        | Auto-delete items older than N days     |
| `DROPZONE_VNC_PORT`    | `5900`     | Local x11vnc port for remote screen     |
| `DROPZONE_WS_PORT`     | `6080`     | websockify bridge port for noVNC        |

## Running as a service

`dropzone.service` (and `x11vnc.service` for remote screen) are sample systemd units.
Edit the `User` and paths to match your setup, then:

```bash
sudo cp dropzone.service /etc/systemd/system/
sudo systemctl enable --now dropzone
```

## Notes

The `data/` directory (uploaded files, SQLite database, and generated secret key) is
git-ignored and stays local to each install.
