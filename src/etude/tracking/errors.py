"""Failures reported by runs without loading their file records."""


class EtudeError(Exception):
    """A refusal with a short code a caller can match on."""

    def __init__(self, code: str, status: int = 409, *, detail: str | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code if detail is None else f"{code}: {detail}")
