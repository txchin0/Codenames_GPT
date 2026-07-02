"""Threading bridge between the synchronous Game engine and the web front-end.

The Codenames :class:`~game.Game` runs a blocking, synchronous loop in a worker
thread.  When it needs a move from a *human* player it calls into one of the
web-backed player classes (see :mod:`web_players`), which in turn block on this
controller until the browser supplies the corresponding action over HTTP.

Nothing in the original framework is modified: the controller simply mediates
between the engine thread and the many short-lived HTTP request threads.
"""

import secrets
import threading
import time


class GameAborted(Exception):
    """Raised inside the engine thread to unwind a game that was replaced."""


class GameController:
    """Shared state + rendezvous point for one game session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._response_ready = threading.Event()
        self._response = None
        self._aborted = False

        # What the engine is currently waiting for a human to provide, or None
        # while a bot is thinking / between turns. Examples:
        #   {"type": "clue",          "team": "Red", "role": "codemaster", "feedback": None}
        #   {"type": "guess",         "team": "Red", "role": "guesser", "clue": "X", "num": 2}
        #   {"type": "keep_guessing", "team": "Red", "role": "guesser", "clue": "X", "num": 2}
        self.pending = None

        # Which seat is currently acting (human or bot) -- drives the UI banner.
        #   {"team": "Red", "role": "codemaster"}
        self.actor = None

        # Game bookkeeping shared with the UI.
        self.game = None
        self.initial_words = []      # pristine board words (before any reveal)
        self.roles = {}              # seat -> "human" | "ai" | "random"
        self.single_team = False
        self.status = "idle"         # idle | running | finished | error
        self.winner = None           # "R" | "B"
        self.error = None
        self.seed = None
        self.reveal_override = False  # spectator "spymaster" toggle
        self.bot_delay = 0.9         # seconds a bot "thinks" (keeps games watchable)
        self.version = 0             # bumps on every state change (poll hint)

        # -- remote multiplayer -------------------------------------------------
        # When any seat is filled by a remote player ("network" kind) the game
        # runs in *network mode*: the key grid is scoped per caller (only the
        # spymaster whose clue turn is pending sees it) and actions must carry a
        # token proving ownership of the seat. Local human/ai/random play leaves
        # these empty and behaves exactly as before.
        self.network_mode = False
        self.claims = {}             # seat -> token
        self.token_seat = {}         # token -> seat
        self.last_seen = {}          # seat -> monotonic timestamp (liveness)
        self.turn_timeout = None     # seconds to wait for a remote move; None = forever

    # -- called from the game (engine) thread ---------------------------------

    def attach_game(self, game, initial_words, seed):
        with self._lock:
            self.game = game
            self.initial_words = list(initial_words)
            self.seed = seed
            self.status = "running"
            self._bump()

    def set_actor(self, team, role):
        """Announce that ``team``'s ``role`` is now thinking (used for bots)."""
        with self._lock:
            self.actor = {"team": team, "role": role}
            self._bump()

    def request(self, payload):
        """Block the engine thread until the browser answers ``payload``."""
        with self._lock:
            if self._aborted:
                raise GameAborted()
            self.pending = dict(payload)
            self.actor = {"team": payload["team"], "role": payload["role"]}
            self._response = None
            self._response_ready.clear()
            self._bump()

        got = self._response_ready.wait(timeout=self.turn_timeout)

        with self._lock:
            if self._aborted:
                raise GameAborted()
            if got and self._response is not None:
                resp = self._response
            else:
                # A remote player never answered in time; fall back to a move the
                # engine already knows how to resolve so the game can advance.
                resp = self._timeout_default(payload)
            self.pending = None
            self._response = None
            self._bump()
        return resp

    @staticmethod
    def _timeout_default(payload):
        """A safe move for when a remote player misses their turn deadline.

        These mirror ``game.py``'s own fallbacks: an empty clue is re-asked/defaulted,
        ``"no comparisons"`` ends the guessing phase, and ``keep=False`` stops guessing.
        """
        kind = payload.get("type")
        if kind == "clue":
            return {"word": "", "number": 1}
        if kind == "guess":
            return {"word": "no comparisons"}
        return {"keep": False}

    def finish(self, winner):
        with self._lock:
            self.status = "finished"
            self.winner = winner
            self.pending = None
            self.actor = None
            self._bump()

    def fail(self, message):
        with self._lock:
            self.status = "error"
            self.error = message
            self.pending = None
            self.actor = None
            self._bump()

    # -- called from web request threads --------------------------------------

    def submit(self, response, token=None):
        """Deliver a player's action back to the waiting engine thread.

        In network mode the ``token`` must own the seat that is currently pending,
        so remote players can only act on their own turn. Local play (no token)
        keeps the original behaviour.
        """
        with self._lock:
            if self.pending is None:
                return False, "The game is not waiting for input right now."
            if self.network_mode:
                seat = self._pending_seat()
                if token is None:
                    return False, "This game requires a seat token."
                if self.claims.get(seat) != token:
                    return False, "It is not your turn (or not your seat)."
                self.last_seen[seat] = time.monotonic()
            self._response = response
            self._response_ready.set()
            return True, None

    def set_reveal(self, value):
        with self._lock:
            self.reveal_override = bool(value)
            self._bump()

    def abort(self):
        """Release a blocked engine thread so a replaced game can exit."""
        with self._lock:
            self._aborted = True
            self._response_ready.set()

    def raise_if_aborted(self):
        """Let an autonomous (bot-only) game notice it has been replaced."""
        if self._aborted:
            raise GameAborted()

    # -- remote multiplayer: seat claims --------------------------------------

    @staticmethod
    def _seat_of(team, role):
        """Map a ``team``/``role`` pair to a seat name, e.g. ('Red','codemaster')."""
        return "{}_{}".format(role, str(team).lower())

    def _pending_seat(self):
        if not self.pending:
            return None
        return self._seat_of(self.pending["team"], self.pending["role"])

    def claim_seat(self, seat, want_token=None):
        """Claim a ``"network"`` seat, returning (ok, token_or_error).

        Passing the current holder's ``want_token`` re-grants the same token so a
        dropped remote player can reconnect and resume.
        """
        with self._lock:
            if seat not in self.roles:
                return False, "Unknown seat: {!r}".format(seat)
            if not self.network_mode:
                return False, "This is not a network game."
            # In a network game every human-played seat is claimed from the lobby --
            # "network" seats and the host's own "human" seats alike. Only bot seats
            # (ai/random) are off-limits.
            if self.roles.get(seat) not in ("network", "human"):
                return False, "Seat {!r} is played by a bot.".format(seat)
            held = self.claims.get(seat)
            if held is not None:
                if want_token and want_token == held:
                    self.last_seen[seat] = time.monotonic()
                    return True, held        # reconnect with the same token
                return False, "Seat {!r} is already taken.".format(seat)
            token = secrets.token_urlsafe(16)
            self.claims[seat] = token
            self.token_seat[token] = seat
            self.last_seen[seat] = time.monotonic()
            self._bump()
            return True, token

    def release_seat(self, token):
        with self._lock:
            seat = self.token_seat.pop(token, None)
            if seat is not None:
                self.claims.pop(seat, None)
                self.last_seen.pop(seat, None)
                self._bump()
            return seat is not None

    def seat_for_token(self, token):
        if not token:
            return None
        with self._lock:
            return self.token_seat.get(token)

    def key_for_token(self, token):
        """The full key grid, but only for the spymaster whose clue is pending.

        Returns ``(ok, key_list_or_error)``. This is the *only* path by which the
        secret grid leaves the host in network mode.
        """
        with self._lock:
            if self.game is None:
                return False, "No game in progress."
            seat = self.token_seat.get(token)
            if seat is None:
                return False, "Unknown or missing token."
            p = self.pending
            if not (p and p["type"] == "clue" and self._pending_seat() == seat):
                return False, "The key is only available on your clue turn."
            return True, list(self.game.key_grid)

    # -- helpers --------------------------------------------------------------

    def _bump(self):
        self.version += 1

    def _should_reveal(self):
        """Whether the key grid may be shown right now.

        A human codemaster must see the key to pick a clue; a human guesser must
        never see it. Between human turns the spectator toggle decides, and once
        the game is over the whole key is revealed.
        """
        if self.status == "finished":
            return True
        p = self.pending
        if p and p["type"] == "clue":
            return True
        if p and p["type"] in ("guess", "keep_guessing"):
            return False
        return self.reveal_override

    def snapshot(self, token=None):
        """Build a JSON-serialisable view of the current game for one caller.

        ``token`` scopes the secret key: in network mode a card's identity is only
        included for the spymaster whose clue turn is pending (plus already-revealed
        cards and, once finished, the whole board). Local play (no network seats)
        keeps the original global reveal so a single shared browser still works.
        """
        with self._lock:
            if self.network_mode:
                seat = self.token_seat.get(token)
                reveal = (self.status == "finished") or (
                    seat is not None and str(seat).startswith("codemaster_"))
            else:
                reveal = self._should_reveal()
            state = {
                "status": self.status,
                "single_team": self.single_team,
                "roles": self.roles,
                "network_mode": self.network_mode,
                "seats_status": {
                    seat: {"kind": kind, "claimed": seat in self.claims}
                    for seat, kind in self.roles.items()
                },
                "you": self.token_seat.get(token),
                "pending": dict(self.pending) if self.pending else None,
                "actor": dict(self.actor) if self.actor else None,
                "winner": self.winner,
                "error": self.error,
                "reveal": reveal,
                "reveal_override": self.reveal_override,
                "seed": self.seed,
                "version": self.version,
            }
            game = self.game
            initial = list(self.initial_words)

        board = []
        remaining = {"Red": 0, "Blue": 0}
        history = []
        if game is not None:
            # list()/copy of a CPython list is atomic w.r.t. other threads, so
            # these snapshots are safe even while the engine mutates the board.
            words_now = list(game.words_on_board)
            key = list(game.key_grid)
            for entry in list(game.get_move_history()):
                history.append(list(entry))

            for i, word in enumerate(initial):
                identity = key[i] if i < len(key) else "Civilian"
                revealed = words_now[i].startswith("*") if i < len(words_now) else False
                cell = {"index": i, "word": word, "revealed": revealed}
                cell["identity"] = identity if (revealed or reveal) else None
                # The authoritative covered marker ("*Red*", ...) so a remote
                # client can rebuild the engine's words_on_board exactly.
                cell["marker"] = words_now[i] if (revealed and i < len(words_now)) else ""
                board.append(cell)

            for i, word in enumerate(words_now):
                if not word.startswith("*") and key[i] in remaining:
                    remaining[key[i]] += 1

        state["board"] = board
        state["history"] = history
        state["remaining"] = remaining
        state["totals"] = {"Red": 9, "Blue": 8, "Civilian": 7, "Assassin": 1}
        return state
