# Contributing to ToolAhead

Issues and focused pull requests are welcome. Please describe how behavior
changes were validated through both cold execution and warm replay.

## Local checks

```bash
python3 -m compileall -q src/toolahead
uv build
python3 .github/scripts/normalize_sdist.py dist/*.tar.gz
python3 .github/scripts/check_distribution.py dist/*.whl dist/*.tar.gz
uvx --from twine twine check dist/toolahead-*
```

Keep speculative mutations out of the live checkout. New replayable tools must
validate exact inputs at serve time and fail open to ordinary execution.
