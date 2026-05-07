"""Glob pattern matching for watch-trigger evaluation.

Used by `dispatch.check_watch_triggers` to decide which jobs fire on a
post-commit hook. Lives separately from `dispatch.py` because it's a
triggering concern, not a prompt-assembly one.
"""

import functools
import re


def any_file_matches(files: list[str], patterns: list[str]) -> bool:
    """Check if any file matches any glob pattern (supports ** recursive)."""
    for pattern in patterns:
        regex = glob_to_regex(pattern)
        for f in files:
            if regex.match(f):
                return True
    return False


@functools.lru_cache(maxsize=256)
def glob_to_regex(pattern: str):
    """Convert a glob pattern to a compiled regex with proper ** support."""
    parts = []
    i = 0
    while i < len(pattern):
        if pattern[i:i+2] == '**':
            parts.append('.*')
            i += 2
            if i < len(pattern) and pattern[i] == '/':
                i += 1
        elif pattern[i] == '*':
            parts.append('[^/]*')
            i += 1
        elif pattern[i] == '?':
            parts.append('[^/]')
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile('^' + ''.join(parts) + '$')
