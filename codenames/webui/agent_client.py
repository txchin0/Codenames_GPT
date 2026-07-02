"""Headless remote agent that plays one seat of a game hosted elsewhere.

Point it at a host's URL (a Cloudflare quick-tunnel link, or ``http://127.0.0.1:8000``
for local testing), tell it which seat to fill and which agent class to run, and it
claims the seat and plays over HTTP -- no browser needed. The agent itself is any
class implementing the framework's ``Codemaster`` / ``Guesser`` interface, so you plug
in the built-in LLM agents *or your own* exactly as you would for terminal play::

    python codenames/webui/agent_client.py \\
        --url https://calm-forest-1234.trycloudflare.com \\
        --seat guesser_blue \\
        --agent players.guesser_GPT:AIGuesser

The engine on the host stays authoritative: this client only supplies the "set" data
it receives over HTTP and posts back the move its agent returns. The spymaster path
additionally fetches the secret key from the host's token-scoped ``/api/keyview``.
"""

import os
import sys
import json
import time
import argparse
import importlib
from urllib import request, error

# Match server.py's path handling so ``players.*``, ``gpt_manager`` and the engine's
# relative resources (players/cm_wordlist.txt) resolve regardless of launch dir.
HERE = os.path.dirname(os.path.abspath(__file__))   # .../codenames/webui
ROOT = os.path.dirname(HERE)                         # .../codenames
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

_DEFAULT_AGENTS = {
    "codemaster": "players.codemaster_GPT:AICodemaster",
    "guesser": "players.guesser_GPT:AIGuesser",
}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self, base_url, token=None):
        self.base = base_url.rstrip("/")
        self.token = token

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def get(self, path):
        req = request.Request(self.base + path, headers=self._headers(), method="GET")
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")

    def post(self, path, body):
        data = json.dumps(body or {}).encode("utf-8")
        req = request.Request(self.base + path, data=data,
                              headers=self._headers(), method="POST")
        try:
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except error.HTTPError as exc:  # 4xx carry a JSON {ok, error} body
            try:
                return json.loads(exc.read() or b"{}")
            except ValueError:
                return {"ok": False, "error": "HTTP {}".format(exc.code)}


# --------------------------------------------------------------------------- #
# Agent loading + state translation
# --------------------------------------------------------------------------- #

def load_agent(import_str, team):
    """Instantiate an agent from a ``module:ClassName`` string (team-aware)."""
    if ":" in import_str:
        module_name, class_name = import_str.split(":", 1)
    else:  # tolerate dotted form: players.guesser_GPT.AIGuesser
        module_name, class_name = import_str.rsplit(".", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(team=team)


def board_words(state):
    """Rebuild the engine's ``words_on_board`` from the state snapshot.

    Uses the authoritative per-cell ``marker`` ("*Red*", ...) for revealed cards and
    the plain word otherwise -- exactly the list the player interface expects.
    """
    cells = sorted(state.get("board", []), key=lambda c: c["index"])
    return [c["marker"] if c.get("marker") else c["word"] for c in cells]


def seat_parts(seat):
    role = "codemaster" if seat.startswith("codemaster") else "guesser"
    team = "Blue" if seat.endswith("blue") else "Red"
    return role, team


# --------------------------------------------------------------------------- #
# Main play loop
# --------------------------------------------------------------------------- #

def play(client, seat, agent, role, poll_ms):
    handled_version = -1        # last state version we already acted on
    clue_key = None             # (clue, num) currently set on the agent this turn
    poll_s = max(0.1, poll_ms / 1000.0)
    print("Joined as {}. Waiting for turns... (Ctrl+C to stop)".format(seat))

    while True:
        try:
            state = client.get("/api/state")
        except (error.URLError, TimeoutError) as exc:
            print("  (state fetch failed: {}; retrying)".format(exc))
            time.sleep(poll_s)
            continue

        status = state.get("status")
        if status == "finished":
            print("Game over. Winner: {}".format(state.get("winner")))
            return
        if status == "error":
            print("Host reported an error: {}".format(state.get("error")))
            return

        pending = state.get("pending")
        version = state.get("version", 0)
        mine = pending and "{}_{}".format(pending["role"], pending["team"].lower()) == seat

        if not (mine and version > handled_version):
            time.sleep(poll_s)
            continue

        words = board_words(state)
        agent.set_move_history([list(m) for m in state.get("history", [])])
        ptype = pending["type"]

        try:
            if ptype == "clue":
                key = _fetch_key(client)
                agent.set_game_state(words, key)
                clue, number = agent.get_clue(feedback=pending.get("feedback"))
                result = client.post("/api/action", {"word": clue, "number": number})
                move = "clue ({}, {})".format(clue, number)
            elif ptype == "guess":
                agent.set_board(words)
                clue_key = _ensure_clue(agent, pending, clue_key)
                guess = agent.get_answer(feedback=pending.get("feedback"))
                result = client.post("/api/action", {"word": guess})
                move = "guess {}".format(guess)
            else:  # keep_guessing
                agent.set_board(words)
                clue_key = _ensure_clue(agent, pending, clue_key)
                keep = bool(agent.keep_guessing())
                result = client.post("/api/action", {"keep": keep})
                move = "keep_guessing {}".format(keep)
        except Exception as exc:  # a broken agent shouldn't wedge the host silently
            print("  agent error while producing {}: {}".format(ptype, exc))
            time.sleep(poll_s)
            continue

        if result.get("ok"):
            handled_version = version
            print("  -> {}".format(move))
        else:
            # Not our turn yet / stale request: leave handled_version alone and re-poll.
            print("  (rejected: {}; re-polling)".format(result.get("error")))
            time.sleep(poll_s)


def _ensure_clue(agent, pending, clue_key):
    """Call ``set_clue`` only when the clue changes, so per-turn guess counters
    in agents like AIGuesser stay coherent across multiple guesses."""
    key = (pending.get("clue"), pending.get("num"))
    if key != clue_key:
        agent.set_clue(pending.get("clue"), pending.get("num"))
    return key


def _fetch_key(client):
    resp = client.get("/api/keyview")
    if not resp.get("ok"):
        raise RuntimeError("could not fetch key: {}".format(resp.get("error")))
    return resp["key"]


def main():
    parser = argparse.ArgumentParser(description="Headless remote Codenames agent.")
    parser.add_argument("--url", required=True, help="host URL (tunnel link or http://127.0.0.1:8000)")
    parser.add_argument("--seat", required=True,
                        choices=["codemaster_red", "guesser_red",
                                 "codemaster_blue", "guesser_blue"])
    parser.add_argument("--agent", default=None,
                        help="agent class as module:ClassName (default: built-in LLM agent for the role)")
    parser.add_argument("--token", default=None, help="reconnect with a previously issued seat token")
    parser.add_argument("--poll-ms", type=int, default=800, help="poll interval in milliseconds")
    args = parser.parse_args()

    role, team = seat_parts(args.seat)
    import_str = args.agent or _DEFAULT_AGENTS[role]
    agent = load_agent(import_str, team)

    client = Client(args.url, token=args.token)
    resp = client.post("/api/join", {"seat": args.seat, "token": args.token})
    if not resp.get("ok"):
        print("Could not claim {}: {}".format(args.seat, resp.get("error")))
        sys.exit(1)
    client.token = resp["token"]
    print("Seat token: {}  (reuse with --token to reconnect)".format(client.token))

    try:
        play(client, args.seat, agent, role, args.poll_ms)
    except KeyboardInterrupt:
        print("\nLeaving seat.")
        client.post("/api/leave", {})


if __name__ == "__main__":
    main()
