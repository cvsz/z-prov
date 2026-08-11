"""Z-Prov."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: pyproject.toml's [project].version, read back
    # from the installed distribution's metadata. Previously this was a
    # hardcoded literal that had to be bumped by hand alongside
    # pyproject.toml on every release -- and had already drifted out of
    # sync at least once (this file said 0.4.1 while pyproject.toml had
    # already moved to 0.4.2). update.sh compares this value at runtime to
    # decide whether an update is needed, so a stale literal here could
    # make it think an already-updated install still needs updating, or
    # vice versa.
    __version__ = version("z-prov")
except PackageNotFoundError:  # pragma: no cover - editable/unbuilt checkout
    __version__ = "0.0.0+unknown"
