__all__ = ["Environment"]


def __getattr__(name):
    if name == "Environment":
        from .environment import Environment

        return Environment
    raise AttributeError(name)
