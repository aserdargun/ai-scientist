"""Shared byte limits for private suite manifests.

Keep this module dependency-light so API startup can enforce the same bound as
the Director without importing its pandas-backed suite materializer.
"""

MAX_SUITE_MANIFEST_BYTES = 96 * 1024**2
