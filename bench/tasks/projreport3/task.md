The folder `projects/` holds several small Python projects. Write `report.json` in the current directory
with one entry per project folder:

```json
{"<project>": {"py_files": 0, "lines": 0, "todos": ["path/in/project.py:LINE: text"], "tests_pass": true}}
```

- `py_files`: the number of `.py` files in the project (including its tests), at any depth.
- `lines`: the total number of lines in those `.py` files.
- `todos`: every comment containing `TODO`, as `relative/path.py:LINE: text after "TODO:"`, sorted.
- `tests_pass`: whether `python3 -m unittest discover -s tests -q`, run inside the project folder, exits with 0.
