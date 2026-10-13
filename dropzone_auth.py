"""
websockify auth plugin for Dropzone's remote-screen bridge.

Only browsers holding a valid `dz_screen` cookie (issued by app.py to a logged-in
admin when they open /screen) are allowed through to the VNC server.  The cookie is
an HMAC-signed, expiring ticket, so the bridge never needs to know any password.

Started automatically by app.py as:
    websockify --auth-plugin dropzone_auth.ScreenTicketAuth --auth-source data/secret.key ...
"""
import hmac
import time
import hashlib

from websockify.auth_plugins import BasePlugin, AuthenticationError


class ScreenTicketAuth(BasePlugin):
    def _secret(self):
        with open(self.source, "rb") as f:
            return f.read()

    def _ticket_ok(self, ticket):
        try:
            exp, sig = ticket.strip().split(".", 1)
            if int(exp) < time.time():
                return False
            want = hmac.new(self._secret(), f"screen|{exp}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(want, sig)
        except (ValueError, OSError):
            return False

    def authenticate(self, headers, target_host, target_port):
        cookie = headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "dz_screen" and self._ticket_ok(v):
                return
        raise AuthenticationError(response_code=403)
