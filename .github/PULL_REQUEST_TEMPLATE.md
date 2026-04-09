## What does this change?

<!-- Brief description of the change and why it's needed -->

## Checklist

- [ ] `python tests.py` passes (all tests green)
- [ ] All files compile: `python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('rates.py','statusline.py','monitor.py')]"`
- [ ] Tested manually with a live Claude Code session
- [ ] Zero new dependencies introduced
- [ ] `MAX_FILE_SIZE` and ANSI palette kept in sync across `statusline.py`, `monitor.py`, `rates.py` (if changed)
- [ ] CHANGELOG.md updated

## Related issues

<!-- Closes #, Fixes # -->
