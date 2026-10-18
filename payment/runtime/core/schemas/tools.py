"""Public policy and tool schema definitions."""


def spec(actor, description, params=(), read=False):
    return dict(actor=actor, description=description, params=params, read=read)
