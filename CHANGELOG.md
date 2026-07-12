# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Nothing yet — open an issue or PR to start the next entry.

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
