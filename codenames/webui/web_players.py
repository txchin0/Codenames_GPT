"""Player classes that let a human play through the web UI.

These mirror the terminal-based ``HumanCodemaster`` / ``HumanGuesser`` in the
framework, but instead of calling ``input()`` they block on a
:class:`~controller.GameController` until the browser supplies the move. They
implement exactly the abstract interfaces from ``players/codemaster.py`` and
``players/guesser.py`` so the engine treats them like any other agent.
"""

from players.codemaster import Codemaster
from players.guesser import Guesser


class WebCodemaster(Codemaster):
    """Codemaster (spymaster) driven by a human through the browser."""

    def __init__(self, team="Red", controller=None):
        super().__init__()
        self.team = team
        self.controller = controller

    def set_game_state(self, words_on_board, key_grid):
        self.words = words_on_board
        self.maps = key_grid

    def get_clue(self, feedback=None):
        resp = self.controller.request({
            "type": "clue",
            "team": self.team,
            "role": "codemaster",
            "feedback": feedback,
        })
        word = str(resp.get("word", "")).strip()
        try:
            number = int(resp.get("number", 1))
        except (TypeError, ValueError):
            number = 1
        return [word, number]


class WebGuesser(Guesser):
    """Guesser (field operative) driven by a human through the browser."""

    def __init__(self, team="Red", controller=None):
        super().__init__()
        self.team = team
        self.controller = controller
        self.clue = None
        self.num = 0

    def set_board(self, words_on_board):
        self.words = words_on_board

    def set_clue(self, clue, num):
        self.clue = clue
        self.num = num

    def get_answer(self, feedback=None):
        resp = self.controller.request({
            "type": "guess",
            "team": self.team,
            "role": "guesser",
            "clue": self.clue,
            "num": self.num,
            "feedback": feedback,
        })
        return str(resp.get("word", "")).strip()

    def keep_guessing(self):
        resp = self.controller.request({
            "type": "keep_guessing",
            "team": self.team,
            "role": "guesser",
            "clue": self.clue,
            "num": self.num,
        })
        return bool(resp.get("keep", False))
