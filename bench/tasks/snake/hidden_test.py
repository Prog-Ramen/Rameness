import ast
import pathlib
import unittest

from snake import SnakeGame


class Hidden(unittest.TestCase):
    def test_initial_state(self):
        g = SnakeGame(20, 15, seed=1)
        self.assertEqual(g.snake, [(10, 7), (9, 7), (8, 7)])
        self.assertEqual((g.direction, g.score, g.alive), ("RIGHT", 0, True))
        self.assertNotIn(g.food, g.snake)
        self.assertTrue(0 <= g.food[0] < 20 and 0 <= g.food[1] < 15)

    def test_move_right_and_turn(self):
        g = SnakeGame(20, 15)
        g.food = (0, 0)
        g.step()
        self.assertEqual(g.snake[0], (11, 7))
        self.assertEqual(len(g.snake), 3)
        g.turn("UP")
        g.step()
        self.assertEqual(g.snake[0], (11, 6))

    def test_reversal_ignored(self):
        g = SnakeGame(20, 15)
        g.food = (0, 0)
        g.turn("LEFT")
        g.step()
        self.assertEqual(g.snake[0], (11, 7))

    def test_eating_grows_and_scores(self):
        g = SnakeGame(20, 15)
        g.food = (11, 7)
        g.step()
        self.assertEqual((len(g.snake), g.score), (4, 1))
        self.assertNotIn(g.food, g.snake)

    def test_wall_kills_and_death_is_final(self):
        g = SnakeGame(5, 5)
        g.food = (0, 0)
        results = [g.step() for _ in range(5)]
        self.assertFalse(g.alive)
        self.assertIn(False, results)
        before = list(g.snake)
        self.assertFalse(g.step())
        self.assertEqual(g.snake, before)

    def test_self_collision(self):
        g = SnakeGame(20, 15)
        g.food = (0, 0)
        g.snake = [(10, 7), (9, 7), (9, 8), (10, 8), (11, 8)]
        g.turn("DOWN")
        g.step()
        self.assertFalse(g.alive)

    def test_seed_is_deterministic(self):
        self.assertEqual(SnakeGame(20, 15, seed=42).food, SnakeGame(20, 15, seed=42).food)

    def test_play_uses_curses(self):
        src = pathlib.Path("play.py").read_text()
        tree = ast.parse(src)
        names = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
                {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertIn("curses", names)
        self.assertIn("SnakeGame", src)

    def test_agent_wrote_tests(self):
        self.assertTrue(list(pathlib.Path(".").glob("test*.py")) or list(pathlib.Path(".").glob("tests/test*.py")))
