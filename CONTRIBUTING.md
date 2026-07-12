# Contributing to Dropzone

First off, thanks for taking the time to contribute — this project grows entirely from people using it, hitting a rough edge, and sending a fix or an idea back.

## Ways to contribute

- **Report a bug** — open an issue with your OS, Python version, and the exact error or `journalctl -u dropzone` output.
- **Suggest a feature** — open an issue describing the use case, not just the feature. "I want X because Y" helps more than "add X."
- **Submit a fix or feature** — see the workflow below.
- **Improve the docs** — typos, unclear steps, and missing setup edge cases are all fair game.

## Development setup

```bash
git clone https://github.com/sxi9/dropzone.git
cd dropzone
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

The app reloads code changes on restart only (no hot-reload by design, to keep the production path and the dev path identical). Restart with `Ctrl+C` then `python app.py` again after edits.

## Project structure

```
dropzone/
├── app.py                 # Entire backend: routes, DB, VNC bridge
├── templates/
│   ├── index.html          # Full UI: styles + template markup + frontend JS, in one file
│   ├── login.html          # PIN entry page
│   └── screen.html          # Full-page noVNC screen viewer
├── static/novnc/            # Vendored noVNC client (do not hand-edit)
├── dropzone.service         # systemd unit template
└── x11vnc.service            # systemd unit template for the screen server
```

There is intentionally **no build step**. The frontend is vanilla JS with a small hand-written template interpreter — if you're used to React/Vue, the mental model is: `computeV()` builds a plain object of everything the template needs, and the template's `{{ }}` / `sc-for` / `sc-if` bind against it. Keep new UI in that same pattern rather than introducing a framework.

## Pull request workflow

1. Fork the repo and create a branch: `git checkout -b feature/short-description`
2. Make your change. Keep it focused — one PR, one concern.
3. Test manually against a real browser on at least one other device (not just `localhost`), since a lot of Dropzone's behavior is specifically about cross-device sync.
4. Update `README.md` if you changed setup steps, environment variables, or added a user-facing feature.
5. Add a line to `CHANGELOG.md` under `Unreleased`.
6. Open the PR against `main` with a clear description of *what* and *why*.

## Code style

- Backend: standard-library-first. Only add a dependency if it earns its place (the way `qrcode` and `websockify` did).
- Frontend: no new frameworks, no build tooling, no npm dependency for the shipped app (noVNC is the one deliberate exception, and it's vendored rather than pulled from a CDN).
- Keep environment variables as the configuration mechanism — no new config file formats.

## Reporting security issues

If you find something that could expose someone's files or PIN over the network, please open an issue marked `security` or reach out directly rather than posting exploit details publicly first.

## Code of conduct

Be respectful, assume good faith, and keep discussion focused on the project. That's the whole policy.
