# Claude memory — Dropzone deployment

Mirrored from the owner's Claude Code memory on 2026-10-19 so any agent or person working
on this repo has the operational facts that are not derivable from the code: where it runs,
how it is deployed, what went wrong before and why. Update this file when the deployment
changes; the memory and this file should say the same thing.

## dropzone-server

_User's self-hosted Dropzone LAN file-share app: Ubuntu host 192.168.29.22, repo github.com/sxi9/dropzone, deploy path and how to restart without sudo_

---
name: dropzone-server
description: "User's self-hosted Dropzone LAN file-share app: Ubuntu host 192.168.29.22, repo github.com/sxi9/dropzone, deploy path and how to restart without sudo"
metadata:
  node_type: memory
  type: project
  originSessionId: 99a3111e-bf4a-4fbb-b22d-17bc6e24066c
  modified: 2026-10-13T10:12:32.095Z
---

Dropzone is the user's Flask LAN file/text share app (repo https://github.com/sxi9/dropzone, only branch and default branch is `claude/github-publishing-35uwrc`, local clone D:\physical_ai\dropzone). It runs on an Ubuntu laptop at http://192.168.29.22:8000 (hostname saibalasundram is the WSL box, NOT this one). SSH works with the user's existing key as `saibala@192.168.29.22` (no passwordless sudo). Deployed folder `~/Downloads/dropzone` is a plain copy, not a git checkout; data lives in `~/Downloads/dropzone/data` (3.4 GB of uploads + dropzone.db, must never be touched). Service `dropzone` has Restart=always, so `pkill -u saibala -f app.py` restarts it without sudo (don't match the ssh command line itself). Deploy = `git archive HEAD | ssh ... tar -x -C ~/Downloads/dropzone`.

On 2026-10-13 v6 was deployed: admin/user accounts (`venv/bin/python app.py set-password admin|user`), ticket-authenticated websockify bridge, hardening. Backup of the old v4 code: `~/Downloads/dropzone.bak-v4-20261013-1540`. Tailscale on the host: node `leakwatch`, IP 100.77.103.75, DNS leakwatch.tail765a4c.ts.net; Dropzone is reachable tailnet-wide at http://100.77.103.75:8000 (no sudo needed). `tailscale serve` needs sudo (or `sudo tailscale set --operator=saibala` once). A Funnel on that node already publishes port 8787 (unrelated 'leakwatch' app) to the public internet. The user plans to host it over Tailscale (README has the section; needs DROPZONE_HTTPS=1 and DROPZONE_TRUST_PROXY=1 behind `tailscale serve`).

**Why:** the user asked for the app to be secured before exposing it via Tailscale and wants all shared files and history preserved across changes.

**How to apply:** never overwrite `data/`; keep the account CLI as the way to set passwords (don't put passwords in chat); verify item counts via /api/state after any deploy. See user saikrishna hardware.

**2026-10-16 feature additions (all deployed, branch claude/github-publishing-35uwrc):** remote desktop via x11vnc (user systemd service `x11vnc.service` runs `x11vnc-start.sh`, which auto-detects the live DISPLAY/XAUTHORITY from gnome-shell — do NOT hardcode :0; the wrapper MUST stay git-executable, `git update-index --chmod=+x`). Dropzone public on Funnel 8443. Screen bridge (websockify 6080) was on Funnel 10000 until 2026-10-18; it is now TAILNET-ONLY on https://leakwatch.tail765a4c.ts.net:8447 (Funnel 10000 now belongs to Bored Chat, see bored chat deployment). The root-owned drop-in /etc/systemd/system/dropzone.service.d/tailscale.conf still says DROPZONE_WS_PUBLIC_PORT=10000 until the user runs the sudo edit to 8447 + daemon-reload + restart dropzone, so the Screen page is broken until then. Session cookie is NOT Secure (SESSION_COOKIE_SECURE=False) so http LAN + https Funnel both work — don't re-enable it. both ufw and firewalld are active; firewalld is the one that blocks, so open ports in both (ufw allow … AND firewall-cmd --permanent --add-port=N/tcp && --reload); open with `sudo firewall-cmd --permanent --zone=public --add-port=N/tcp && reload`. Features now in the app: visitor logging + experimental browser collectors (admin Visitors tab, data/captures/), **per-user privileges & limits** (admin Accounts→Privileges: download/upload/notes/delete/chat/screen/fetch_url/see_all toggles, max_upload_mb/quota_mb/max_items/lan_only limits, expiry for guest accounts; stored in users.json perms/limits/expires; files table has owner_user col), non-blocking corner upload card with receiver-visible progress (/api/upload-progress, in-memory _uploads), and resumable/parallel uploads (/api/upload-chunk, data/chunks/). Test suites in scratchpad (test_perms.py etc) hit a harness hang in finally (srv.stdout.read) but all checks print before it — read PASS/FAIL lines directly. Stray test servers accumulate on Windows; kill by port before re-running. websockify forks one worker per client connection — counting processes overcounts; it's not a duplicate-bridge bug.

**2026-10-19 (deployed, commit 9ee3351 on claude/github-publishing-35uwrc):** public-link control — per-file `public`/`pin` pills with a manage dialog (Copy / Stop sharing publicly / Remove from all links), Public filter, multi-select "Stop public", Links panel (every live link: badge, files, downloads, expiry dropdown, End link), timed links (share dialog default 7 days; `/api/share` takes `expires_in`, `/api/share/<token>/expires` changes it; expired links 404 + purged), `POST /api/unshare {ids, scope}`. Multi-select zip download and multi-file links already existed (commit 491496c). Restart without killing the ssh shell: `pkill -u saibala -f "dropzone/app[.]py"` (the bracket keeps the regex from matching its own command line). Local UI check recipe: scratch venv with Flask+qrcode, `DROPZONE_DATA` pointing at a temp dir, accounts via `cli(["set-password", ...])` with getpass monkeypatched, Flask test client for API smoke tests (no server, no harness hang). DROPZONE_PUBLIC_SHARE is NOT set on the box, but new-style `/s/<token>` links work without login anyway (the flag only gates legacy per-file tokens).

## tailscale-panel

_Standalone Tailscale Panel project (repo sxi9/tailscale-panel, private) deployed on the Ubuntu host at port 8090 as a systemd user service; Dropzone is public via Funnel on 8443_

Tailscale Panel: standalone Flask control panel for the Ubuntu host's Tailscale node (local clone D:\physical_ai\tailpanel, private GitHub repo https://github.com/sxi9/tailscale-panel, branch main). Deployed at `~/tailpanel` on saibala@192.168.29.22, running as a **user** systemd unit (`systemctl --user ... tailpanel`, linger is enabled, no sudo needed), port 8090, own accounts in `~/tailpanel/data/users.json` (`venv/bin/python app.py set-password admin`). Deploy = `git archive HEAD | ssh ... tar -x -C ~/tailpanel` then `systemctl --user restart tailpanel`. ufw is active on the host; 8090 is opened only for 192.168.29.0/24 and 100.64.0.0/10 (setup script `~/tailpanel-setup.sh`). saibala is the tailscale operator, so serve/funnel work without sudo.

As of 2026-10-13 Dropzone is PUBLIC via Funnel at https://leakwatch.tail765a4c.ts.net:8443 (drop-in `/etc/systemd/system/dropzone.service.d/tailscale.conf` sets DROPZONE_HTTPS=1 and DROPZONE_TRUST_PROXY=1). Port 443 Funnel belongs to the user's ~/leakwatch app (run.py, port 8787). Port 10000 Funnel = Bored Chat (Caddy 8080) since 2026-10-18; 8446 tailnet-only = whatsapp-headless 3001; 8447 tailnet-only = Dropzone screen bridge 6080. Funnel only reaches the internet on 443/8443/10000.

**Why:** the user wants one place to see what the Ubuntu box exposes and to toggle hosted services between tailnet-only and public.

**How to apply:** never publish the panel itself; test changes with the fake-tailscale shim approach (scratch test suite) before deploying. See dropzone server.

