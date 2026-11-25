"""Expose the public TechMC Environment class through a lazy import."""

__all__ = ["Environment"]


def __getattr__(name):
    """Load Environment when requested, raising AttributeError for other names."""

    if name == "Environment":
        from .environment import Environment

        return Environment
    raise AttributeError(name)
