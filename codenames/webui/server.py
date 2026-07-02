"""A small, dependency-free web server for playing Codenames in the browser.

Runs the unmodified :class:`~game.Game` engine in a background thread and bridges
human turns to a single-page UI over HTTP. Start it from the ``codenames``
directory (or the project root) with::

    uv run python codenames/webui/server.py           # from the project root
    uv run python webui/server.py                     # from the codenames dir

then open http://127.0.0.1:8000 in a browser.
"""

import os
import sys
import json
import socket
import argparse
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Resolve paths so the engine's relative resources (players/cm_wordlist.txt) and
# imports (game, players.*) work regardless of where the server is launched.
HERE = os.path.dirname(os.path.abspath(__file__))   # .../codenames/webui
ROOT = os.path.dirname(HERE)                         # .../codenames
STATIC_DIR = os.path.join(HERE, "static")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from game import Game                                # noqa: E402
from controller import GameController, GameAborted   # noqa: E402
from web_players import WebCodemaster, WebGuesser    # noqa: E402
from bots import RandomCodemaster, RandomGuesser, make_notifying  # noqa: E402

# The LLM-backed agents pull in the OpenAI client; keep them optional so the UI
# still runs (human/random only) if that import ever fails.
try:
    from players.codemaster_GPT import AICodemaster
    from players.guesser_GPT import AIGuesser
    AI_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - defensive
    AICodemaster = AIGuesser = None
    AI_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

SEATS = ("codemaster_red", "guesser_red", "codemaster_blue", "guesser_blue")

CONTROLLER = None
GAME_THREAD = None
TURN_TIMEOUT = None      # seconds a remote player has to move; set from --turn-timeout
_start_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Game construction / worker thread
# --------------------------------------------------------------------------- #

def _sanitize_seed(raw):
    if isinstance(raw, bool):
        return "time"
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "" or raw.lower() == "time":
            return "time"
        try:
            return int(raw)
        except ValueError:
            return "time"
    return "time"


def _build_player(kind, role, controller):
    """Return ``(player_class, kwargs)`` for a seat.

    ``role`` is ``"codemaster"`` or ``"guesser"``; ``kind`` is
    ``"human"``, ``"ai"`` or ``"random"``.
    """
    kind = (kind or "random").lower()
    if kind in ("human", "network"):
        # A "network" seat is filled by a remote human or agent over HTTP; from
        # the engine's point of view it is identical to a local human seat -- it
        # blocks on the controller until an action arrives. The only difference
        # (token-scoped key + action auth) lives in the controller/HTTP layer.
        cls = WebCodemaster if role == "codemaster" else WebGuesser
        return cls, {"controller": controller}
    if kind == "ai":
        base = AICodemaster if role == "codemaster" else AIGuesser
        return make_notifying(base, controller, role), {}
    base = RandomCodemaster if role == "codemaster" else RandomGuesser
    return make_notifying(base, controller, role), {}


def _game_worker(controller, seats, seed, single_team):
    try:
        cm_r, cmr_kw = _build_player(seats["codemaster_red"], "codemaster", controller)
        g_r, gr_kw = _build_player(seats["guesser_red"], "guesser", controller)
        cm_b, cmb_kw = _build_player(seats["codemaster_blue"], "codemaster", controller)
        g_b, gb_kw = _build_player(seats["guesser_blue"], "guesser", controller)

        game = Game(cm_r, g_r, cm_b, g_b,
                    seed=seed, do_print=True, do_log=False, game_name="webui",
                    single_team=single_team,
                    cmr_kwargs=cmr_kw, gr_kwargs=gr_kw,
                    cmb_kwargs=cmb_kw, gb_kwargs=gb_kw)
        controller.attach_game(game, game.words_on_board, game.seed)
        game.run()
        controller.finish(game.game_winner)
    except GameAborted:
        pass  # a new game replaced this one; unwind quietly
    except Exception as exc:
        traceback.print_exc()
        msg = f"{type(exc).__name__}: {exc}"
        if AI_IMPORT_ERROR and ("ai" in seats.values()):
            msg += "  (LLM agents require the local model server to be running.)"
        controller.fail(msg)


def start_new_game(config):
    global CONTROLLER, GAME_THREAD

    seats = dict(config.get("seats") or {})
    for seat in SEATS:
        seats.setdefault(seat, "random")
        if seats[seat] not in ("human", "ai", "random", "network"):
            return False, f"Invalid choice for {seat}: {seats[seat]!r}"

    single_team = bool(config.get("single_team", False))
    if single_team:
        # In the single-team track only the red team plays.
        seats["codemaster_blue"] = "random"
        seats["guesser_blue"] = "random"

    if AICodemaster is None and "ai" in seats.values():
        return False, f"LLM agents are unavailable ({AI_IMPORT_ERROR})."

    seed = _sanitize_seed(config.get("seed", "time"))

    with _start_lock:
        if CONTROLLER is not None:
            CONTROLLER.abort()
        controller = GameController()
        controller.roles = seats
        controller.single_team = single_team
        # Any remote seat switches the game into network mode (token-scoped key
        # + per-seat action auth). Purely local games behave exactly as before.
        controller.network_mode = any(k == "network" for k in seats.values())
        controller.turn_timeout = TURN_TIMEOUT
        CONTROLLER = controller
        GAME_THREAD = threading.Thread(
            target=_game_worker, args=(controller, seats, seed, single_team),
            daemon=True)
        GAME_THREAD.start()
    return True, None


def submit_action(data, token=None):
    if CONTROLLER is None:
        return False, "No game in progress."
    return CONTROLLER.submit(data or {}, token=token)


def join_seat(data):
    if CONTROLLER is None:
        return False, "No game in progress."
    seat = (data or {}).get("seat")
    want = (data or {}).get("token")
    ok, result = CONTROLLER.claim_seat(seat, want_token=want)
    if not ok:
        return False, result
    role = "codemaster" if str(seat).startswith("codemaster") else "guesser"
    team = "Blue" if str(seat).endswith("blue") else "Red"
    return True, {"token": result, "seat": seat, "role": role, "team": team}


def leave_seat(token):
    if CONTROLLER is None or not token:
        return {"ok": False}
    return {"ok": CONTROLLER.release_seat(token)}


def key_view(token):
    if CONTROLLER is None:
        return False, "No game in progress."
    return CONTROLLER.key_for_token(token)


def current_state(token=None):
    if CONTROLLER is None:
        return {"status": "idle", "board": [], "history": [],
                "ai_available": AICodemaster is not None}
    state = CONTROLLER.snapshot(token=token)
    state["ai_available"] = AICodemaster is not None
    return state


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodenamesUI/1.0"

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename, content_type):
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as fh:
                self._send(200, fh.read(), content_type)
        except OSError:
            self._send(404, {"error": f"{filename} not found"})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def _token(self):
        """Seat token from an ``Authorization: Bearer`` header or ``?token=`` query."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip() or None
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        return parse_qs(query).get("token", [None])[0]

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in _STATIC_FILES:
            return self._serve_static(*_STATIC_FILES[route])
        if route == "/api/state":
            return self._send(200, current_state(token=self._token()))
        if route == "/api/keyview":
            ok, result = key_view(self._token())
            if not ok:
                return self._send(403, {"ok": False, "error": result})
            return self._send(200, {"ok": True, "key": result})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/new_game":
            ok, err = start_new_game(self._read_json())
            return self._send(200 if ok else 400, {"ok": ok, "error": err})
        if route == "/api/join":
            ok, result = join_seat(self._read_json())
            if not ok:
                return self._send(400, {"ok": False, "error": result})
            payload = {"ok": True}
            payload.update(result)
            return self._send(200, payload)
        if route == "/api/leave":
            return self._send(200, leave_seat(self._token()))
        if route == "/api/action":
            ok, err = submit_action(self._read_json(), token=self._token())
            return self._send(200 if ok else 400, {"ok": ok, "error": err})
        if route == "/api/reveal":
            if CONTROLLER is not None:
                CONTROLLER.set_reveal(bool(self._read_json().get("reveal", False)))
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # keep the console clean; the engine already prints game progress


def _port_in_use(host, port):
    """True if something is already listening on ``host:port``.

    We *connect* rather than trusting bind() to fail: on Windows the server sets
    ``allow_reuse_address`` (SO_REUSEADDR), which lets a second process silently
    share the port -- exactly the trap that leaves a stale server answering.
    """
    target = host if host not in ("", "0.0.0.0") else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((target, port)) == 0


def main():
    global TURN_TIMEOUT
    parser = argparse.ArgumentParser(description="Codenames web UI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="open a browser on start")
    parser.add_argument("--tunnel", action="store_true",
                        help="expose the server publicly via a Cloudflare quick tunnel "
                             "(requires the 'cloudflared' binary on PATH)")
    parser.add_argument("--turn-timeout", type=float, default=None,
                        help="seconds a remote player has to move before the engine "
                             "plays a safe default (default: wait forever)")
    args = parser.parse_args()

    TURN_TIMEOUT = args.turn_timeout

    if _port_in_use(args.host, args.port):
        print(f"\nERROR: port {args.port} is already in use -- another server is "
              f"probably still running.\n"
              f"Stop it first (Ctrl+C in its terminal), or start this one on a "
              f"different port with --port <n>.\n"
              f"On Windows a stale server can silently share the port; to clear "
              f"any lingering ones:\n"
              f"  Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              f"Where-Object {{ $_.CommandLine -match 'server\\.py' }} | "
              f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}\n")
        sys.exit(1)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"\nCodenames web UI running at {url}  (Ctrl+C to stop)\n")

    tunnel = None
    if args.tunnel:
        from tunnel import start_tunnel  # local import; only needed with --tunnel
        tunnel = start_tunnel(args.port)

    if args.open:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
    finally:
        if tunnel is not None:
            tunnel.stop()


if __name__ == "__main__":
    main()
