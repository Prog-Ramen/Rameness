Build a playable Snake game in Python in the current directory.

Requirements:
- `snake.py` defines `class SnakeGame` with constructor `SnakeGame(width=20, height=15, seed=0)`:
  - `snake`: list of (x, y) tuples, head first. The snake starts with length 3 in the middle of the board,
    heading RIGHT: head at (width // 2, height // 2), body extending to the left.
  - `direction`: one of "UP", "DOWN", "LEFT", "RIGHT"; starts as "RIGHT". `turn(direction)` changes the
    direction but ignores a direct reversal (e.g. LEFT while moving RIGHT). UP decreases y.
  - `food`: an (x, y) tuple on a cell not occupied by the snake, chosen with `random.Random(seed)`.
  - `score` (starts at 0) and `alive` (starts True).
  - `step()`: moves the snake one cell in the current direction. Moving onto the food grows the snake by
    one cell, adds 1 to `score` and places new food on a free cell. Moving off the board or into the
    snake's own body sets `alive` to False. Returns `alive`. Once dead, `step()` changes nothing.
- `play.py`: a terminal UI using the `curses` module (arrow keys to steer, q to quit) built on `SnakeGame`.
- Write unit tests for the game logic and make sure they pass.
