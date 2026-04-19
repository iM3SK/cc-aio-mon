## What does this change?

<!-- Brief description of the change and why it's needed -->

## Checklist

- [ ] `python3 tests.py` (or `py tests.py` on Windows) passes (all tests green)
- [ ] All files compile: `python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('shared.py','statusline.py','monitor.py','update.py','pulse.py')]"` (use `py` on Windows)
- [ ] Tested manually with a live Claude Code session
- [ ] Zero new dependencies introduced
- [ ] Any new shared constant / helper / ANSI attribute lives in `shared.py` (not duplicated in `statusline.py` / `monitor.py` / `pulse.py` / `update.py`)
- [ ] CHANGELOG.md updated

## Related issues

<!-- Closes #, Fixes # -->
