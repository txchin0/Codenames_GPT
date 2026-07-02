"""Optional Cloudflare *quick tunnel* helper for the web UI.

A quick tunnel needs no Cloudflare account and no domain: the ``cloudflared``
binary opens an outbound connection to Cloudflare and hands back a public
``https://<random>.trycloudflare.com`` URL that forwards to the local server.
That is all remote multiplayer needs -- share the URL and the other player joins.

The server never *depends* on this module; it is imported only when the user
passes ``--tunnel``. If ``cloudflared`` is not installed we print an install hint
and keep serving locally.
"""

import re
import shutil
import subprocess
import threading

# cloudflared prints the assigned hostname to stderr, e.g.
#   |  https://calm-forest-1234.trycloudflare.com   |
_URL_RE = re.compile(r"https://[-\w]+\.trycloudflare\.com")

_INSTALL_HINT = (
    "cloudflared not found on PATH. Install it to expose the game publicly:\n"
    "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
    "  (Windows: 'winget install --id Cloudflare.cloudflared', or download the .exe)\n"
    "The server is still running locally; multiplayer just needs a public URL to share."
)


class Tunnel:
    """A running cloudflared child process exposing ``127.0.0.1:<port>``."""

    def __init__(self, proc):
        self._proc = proc
        self.url = None
        self._url_ready = threading.Event()

    def wait_for_url(self, timeout=30):
        self._url_ready.wait(timeout=timeout)
        return self.url

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def start_tunnel(port, host="127.0.0.1"):
    """Launch a cloudflared quick tunnel to ``http://host:port``.

    Returns a :class:`Tunnel` (whose ``.stop()`` should be called on shutdown), or
    ``None`` if ``cloudflared`` is not installed.
    """
    if shutil.which("cloudflared") is None:
        print("\n" + _INSTALL_HINT + "\n")
        return None

    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://{host}:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tunnel = Tunnel(proc)

    def _pump():
        # cloudflared is chatty; we only surface the public URL, then stay quiet.
        for line in proc.stdout:
            if tunnel.url is None:
                match = _URL_RE.search(line)
                if match:
                    tunnel.url = match.group(0)
                    tunnel._url_ready.set()
                    print("\n" + "=" * 60)
                    print("  Public game URL (share this with the other player):")
                    print("    " + tunnel.url)
                    print("=" * 60 + "\n")

    threading.Thread(target=_pump, daemon=True).start()

    if tunnel.wait_for_url(timeout=30) is None:
        print("\nTunnel started but no public URL was detected yet; "
              "watch the console -- cloudflared may still be connecting.\n")
    return tunnel
