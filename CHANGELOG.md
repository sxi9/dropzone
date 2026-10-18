# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- **Admin and user accounts.** `python app.py set-password admin|user` creates salted PBKDF2 (600k rounds) password hashes in `data/users.json`; the page now asks for a username and password. Users can browse, upload, share text, chat and delete their own items. Admins additionally get the storage bar and panel, cleanup, server-side URL fetch, device renaming for any device, deleting anything, and the remote screen viewer.
- `DROPZONE_ADMIN_PASSWORD` / `DROPZONE_USER_PASSWORD` to bootstrap accounts from the environment, `DROPZONE_SESSION_HOURS`, `DROPZONE_HTTPS`, `DROPZONE_TRUST_PROXY`, `DROPZONE_PUBLIC_SHARE`.
- **Admin panel** at `/admin` (Admin button in the header): segmented storage meter with counts and cleanup, account management (add / reset password / change role / remove, with guards against locking yourself out), a *directory library* of server folders, and a file manager over them (browse, upload, download file or folder-as-zip, new folder, rename, move, delete, share-to-feed). All paths are confined to the configured folders.
- The sidebar storage bar is now a labelled two-tone meter showing Dropzone's share of the disk.
- **Password** button in the header: change your own password from the UI (asks for the current one; signs out your other devices).
- **Experimental collectors** (Visitors tab, each off by default, need logging + browser details, HTTPS): WebRTC-exposed IPs, battery, GPU model, a canvas/WebGL device fingerprint, camera/mic device counts, notification-permission, motion sensors, a camera still, a microphone clip, and clipboard contents. The last four prompt the visitor through the browser's own permission dialog and recording indicator; captures are stored under data/captures and viewable only by admins. Cleared with the visitor log.
- **Precise GPS location** (Visitors tab, off by default, needs browser details + HTTPS): asks each visitor's browser for their exact position via the Geolocation API. The browser shows the visitor its own permission prompt and nothing is sent unless they allow it; denials are logged too. Shown in the visitor table and log as a clickable OpenStreetMap link.
- **Visitor log** (admin panel → Visitors, off by default): records IP, origin (LAN/tailnet/public), account, request and result, browser/OS/device parsed from the User-Agent, language, referer, failed logins, optional ip-api.com geolocation of public addresses (cached 30 days, looked up in the background), and an optional browser beacon (screen, viewport, timezone, languages, platform, touch, cores, memory, connection, UA client hints). Configurable retention, per-address summary, filter by address, clear button. Polling endpoints are never logged.
- `DROPZONE_WS_PUBLIC_PORT`: the screen viewer uses this bridge port when the page was opened over HTTPS, so the remote screen works through a Funnel route. `x11vnc.service` now uses `-auth guess` (works with GDM) and restarts always.
- README section on hosting through Tailscale.

### Security
- **Screen bridge is now authenticated.** websockify used to accept any connection on port 6080 from anyone on the network, straight into the VNC server. It now runs with the `dropzone_auth.ScreenTicketAuth` plugin and only admits browsers carrying a signed, expiring `dz_screen` cookie that Dropzone issues to admins.
- Login throttling: 5 failed attempts from one address lock that address out for 5 minutes. Comparisons are constant-time and unknown usernames cost the same as wrong passwords.
- Session cookies are `HttpOnly` + `SameSite=Lax` (and `Secure` with `DROPZONE_HTTPS=1`); changing a password invalidates that account's existing sessions.
- Every state-changing request is checked against the `Origin`/`Referer` host, blocking cross-site requests.
- `X-Forwarded-For` is no longer trusted blindly (it let anyone impersonate another device's address and read items sent to it); enable `DROPZONE_TRUST_PROXY=1` behind a real proxy.
- Uploaded HTML/XML/JavaScript is served as a download instead of rendered in the app's origin; SVG previews are sandboxed. Standard hardening headers on every response.
- Share links (`/s/<token>`) now require a login unless `DROPZONE_PUBLIC_SHARE=1`.
- `data/secret.key` and `data/users.json` are written with mode 600.
- Raw file, preview and zip endpoints respect send-to-device visibility.

### Changed
- Legacy `DROPZONE_PIN` still works: it acts as the password of the `user` account until a real user account is created.

### Fixed
- Stray "[object Object]" next to the burn-after-read chip.
- **Copy buttons on notes and share links** now work over plain HTTP on the LAN. The Clipboard API is only available in secure contexts (HTTPS or localhost), so the old code silently did nothing while still showing "Copied". Added an `execCommand("copy")` fallback and an honest toast when the browser blocks copying.

### Shareable links
- Share one file or a bunch of selected files with a link. **Public** links let anyone download with no password; **private** links need a PIN you set. Either way the link exposes only those files, nothing else in the app.
- Per-file "Share" button and a "Share link" action in multi-select, each opening a public/PIN chooser that copies the link. The recipient sees a clean download page (and a zip-all for multiple files). Admins/owners can list and revoke links (/api/share, /api/shares). Download perm required to create a link.

### Uploads
- The upload progress is now a small corner card instead of a full-screen overlay, so you can keep browsing and checking files while files go up.
- The person a file is sent to (or everyone, for an untargeted share) sees the incoming file's live progress.
- Multiple files upload in parallel, and files over 8 MB upload in 4 MB chunks that automatically resume after a dropped connection instead of restarting (new /api/upload-chunk endpoint).

### Per-user privileges & limits
- Admins can now set, per account, what each user may do: download/preview files, upload, share text, delete own items, chat, use the live screen, pull URLs, and whether they see everyone's items or only their own. Edited from Accounts -> Privileges, with Read-only / Upload-only / Screen-access presets.
- Per-account limits: max upload size, storage quota, max number of items, and a "private networks only" switch that blocks the account from the public internet.
- Guest accounts: set an expiry date after which the account can no longer sign in.
- Enforced server-side on every endpoint; the main page hides controls a user isn't allowed to use. Uploads now record the owning account (owner_user) for quota and ownership.

## [5.0.0] — Home hub release

### Added
- **Remote screen viewer** — real VNC access to the host machine's desktop (mouse, keyboard, Ctrl-Alt-Del) via a vendored noVNC client and a built-in WebSocket bridge to `x11vnc`.
- **Labels/tags** — one-click filter chips to organize the feed beyond a flat list.
- **Storage panel** — largest files at a glance with one-tap cleanup (older than 30 days / all unpinned).
- **Clickable activity digest** — "Today: X files · Y notes · Z messages" now jumps straight to the relevant view.
- **Slim remote mode** — automatically lightens the UI (no image thumbnails, calmer animation) when accessed remotely, e.g. over Tailscale.

### Fixed
- Rendering bug affecting lines combining two bound values (e.g. "2.4 MB · 5m ago").

## [4.0.0] — Full feature pass

### Added
- **Per-item share links** — copyable direct links to individual files.
- **Multi-select** with **bulk delete** and **download-selected-as-zip**.
- **Pin/favorite** items to keep them at the top of the feed.
- **Paste-to-upload** — paste a screenshot or copied file directly onto the page.
- **Folder drag-and-drop**, preserving relative paths in filenames.
- **Voice memos** recorded from the browser microphone.
- **Burn-after-read notes** — self-deleting secrets, ideal for passwords.
- **Per-item expiry**, independent of the global auto-expiry setting.
- **Send-to-device** — target a single device instead of broadcasting to all.
- **Upload from URL** — the server fetches the file, not the requesting device.
- **Device renaming**, device type detection, and a live devices sidebar.
- **Activity feed** and **cross-device chat**.
- **In-page previews** for images, video, audio, PDFs, and text.
- **Instant sync** via Server-Sent Events, replacing polling as the primary update path.

## [3.0.0] — Custom UI

### Changed
- Replaced the original interface with a fully custom-designed UI (glassmorphism aesthetic, animated background, dedicated sidebar).
- Removed all third-party frontend runtime/framework dependencies in favor of a small hand-written template interpreter — keeping the app dependency-free and fully offline-capable.

## [2.0.0] — Reliability pass

### Fixed
- Documented and resolved the common causes of "reachable by IP but not by port" on Ubuntu: conda/venv Python conflicts, port collisions from stray manual runs, and `firewalld`'s zone-based rules silently blocking non-default ports even when `ufw` reports inactive.

### Added
- systemd service for always-on operation with automatic restart.

## [1.0.0] — Initial release

### Added
- Core drop zone: drag-and-drop file upload, text/clipboard sharing, live searchable feed with All/Files/Text filters.
- Image thumbnails, QR code for onboarding new devices.
- Optional shared PIN and optional global auto-expiry.
- Light and dark themes.
