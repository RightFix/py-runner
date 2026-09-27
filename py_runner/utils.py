"""Shared helpers. Standard library only."""


def try_import(name):
    try:
        import importlib

        return importlib.import_module(name)
    except ImportError:
        return None


# Backwards-compat alias (old private name).
_try_import = try_import


def set_address_space_limit(mb: float):
    """Cap this process's virtual address space (Unix-only, stdlib only).

    Uses resource.RLIMIT_AS when available; silently returns False where
    unsupported (e.g. Windows). Allocations beyond the cap raise
    MemoryError inside the cell instead of OOM-killing the host — useful
    guardrail when serving untrusted notebooks. Process-wide by nature:
    call once at startup, not per cell.

    Returns True if the limit was applied.
    """
    resource = try_import("resource")
    if resource is None:
        return False
    try:
        limit = int(mb * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return True
    except Exception:
        return False
