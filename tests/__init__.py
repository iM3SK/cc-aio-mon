# SPDX-License-Identifier: MIT
"""cc-aio-mon test suite."""

import os
import tempfile

# Hermetic Fable reader: statusline.main() reads Claude Code's `.claude.json`
# and may spawn a detached `claude -p` usage refresh. Point every test at a
# config dir that does not exist and switch the refresher to read-only, so the suite never
# reads the developer's real account data or starts a real `claude` process.
# Tests that exercise the reader/refresher pass explicit paths and TTLs.
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tempfile.gettempdir(), "cc-aio-mon-test-no-claude-config")
os.environ["CC_AIO_MON_FABLE_REFRESH_SEC"] = "0"
