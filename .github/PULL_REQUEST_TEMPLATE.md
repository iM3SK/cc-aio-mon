## What does this change?

<!-- Brief description of the change and why it's needed -->

## Checklist

- [ ] `python3 tests.py` (or `py tests.py` on Windows) passes (all tests green)
- [ ] All files compile: `python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('shared.py','statusline.py','monitor.py','update.py','pulse.py')]"` (use `py` on Windows)
- [ ] Tested manually with a live Claude Code session
- [ ] Zero new dependencies introduced
- [ ] `MAX_FILE_SIZE` and ANSI palette kept in sync across `statusline.py` and `monitor.py` (if changed)
- [ ] CHANGELOG.md updated

## Related issues

<!-- Closes #, Fixes # -->
