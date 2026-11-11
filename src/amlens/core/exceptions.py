"""Custom exception hierarchy. Raise these instead of bare Exception."""


class AMLensError(Exception):
    """Base class for all AMLens errors."""


class ConfigError(AMLensError):
    """Missing or invalid configuration."""


class DatabaseUnavailableError(AMLensError):
    """A database could not be reached."""
