"""Lightweight, dependency-free bots and a "thinking" wrapper for the web UI.

The framework's ``AICodemaster`` / ``AIGuesser`` need a local LLM server. The
random bots here let the UI be played and demoed with no external services at
all -- handy for trying human-vs-bot without setting up ``llama.cpp``.

``make_notifying`` wraps *any* bot so it announces "X is thinking" to the
controller (and pauses briefly) before moving, which keeps games watchable.
"""

import random
import time

from controller import GameAborted
from players.codemaster import Codemaster
from players.guesser import Guesser

# Generic, board-neutral words the random codemaster can draw from. The engine
# still validates every clue; keeping these generic makes accidental rule
# violations (a clue that is a substring of a board word) rare.
_CLUE_BANK = [
    "OCEAN", "MOUNTAIN", "SHADOW", "ENGINE", "GARDEN", "SIGNAL", "PLANET",
    "RIVER", "CASTLE", "THUNDER", "COMPASS", "LANTERN", "HARVEST", "ORBIT",
    "PUZZLE", "VELVET", "MIRROR", "ANCHOR", "MEADOW", "CIRCUIT", "FEATHER",
    "GLACIER", "JUNGLE", "MARKET", "PILLAR", "ROCKET", "SADDLE", "TEMPLE",
    "VOYAGE", "WHISTLE", "BEACON", "CANYON", "DIAMOND", "FALCON", "GRAVITY",
]


class RandomCodemaster(Codemaster):
    """Gives a random, rule-valid clue. No external services required."""

    def __init__(self, team="Red", **kwargs):
        super().__init__()
        self.team = team
        self.words = []
        self.maps = []

    def set_game_state(self, words_on_board, key_grid):
        self.words = words_on_board
        self.maps = key_grid

    def _remaining_own(self):
        return [w for w, ident in zip(self.words, self.maps)
                if not w.startswith("*") and ident == self.team]

    def get_clue(self, feedback=None):
        board = [w.upper() for w in self.words if not w.startswith("*")]
        candidates = [c for c in _CLUE_BANK
                      if not any(c in w or w in c for w in board)]
        clue = random.choice(candidates) if candidates else "HINT"
        own_left = max(1, len(self._remaining_own()))
        number = random.randint(1, min(3, own_left))
        return [clue, number]


class RandomGuesser(Guesser):
    """Guesses random remaining words. No external services required."""

    def __init__(self, team="Red", **kwargs):
        super().__init__()
        self.team = team
        self.words = []
        self.clue = None
        self.num = 0
        self._guesses = 0

    def set_board(self, words_on_board):
        self.words = words_on_board

    def set_clue(self, clue, num):
        self.clue = clue
        self.num = num
        self._guesses = 0

    def _remaining(self):
        return [w for w in self.words if not w.startswith("*")]

    def get_answer(self, feedback=None):
        remaining = self._remaining()
        self._guesses += 1
        return random.choice(remaining) if remaining else None

    def keep_guessing(self):
        # Roughly honour the clue budget, with a little randomness for flavour.
        if self.num and self._guesses >= self.num:
            return False
        return random.random() < 0.7


def make_notifying(base_cls, controller, role):
    """Return a subclass of ``base_cls`` that reports thinking to the UI.

    ``role`` is ``"codemaster"`` or ``"guesser"``. The wrapper sets the current
    actor and pauses ``controller.bot_delay`` seconds before each move so bot
    turns are visible in the browser rather than flashing past.
    """

    def _pause(self):
        controller.raise_if_aborted()   # stop promptly if a new game replaced us
        controller.set_actor(self.team, role)
        if controller.bot_delay:
            time.sleep(controller.bot_delay)

    if role == "codemaster":
        class _Notifying(base_cls):
            def get_clue(self, feedback=None):
                _pause(self)
                return super().get_clue(feedback=feedback)
    else:
        class _Notifying(base_cls):
            def get_answer(self, feedback=None):
                _pause(self)
                return super().get_answer(feedback=feedback)

            def keep_guessing(self):
                _pause(self)
                return super().keep_guessing()

    _Notifying.__name__ = "Notifying" + base_cls.__name__
    _Notifying.__qualname__ = _Notifying.__name__
    return _Notifying
