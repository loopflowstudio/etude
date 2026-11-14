"""The recipe's planned stop, shared by tracking and the lightweight function caller."""


class Stopped(Exception):
    """State is saved; call the recipe again to continue toward its target."""
