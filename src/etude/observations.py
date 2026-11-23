"""Attempt-scoped authority and portable run evidence shared by publisher and receiver."""

import base64
import hmac
import os
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from etude.block_file import NAME
from etude.errors import EtudeError, LauncherError
from etude.identity import SAFE_NAME
from etude.launch.records import Cause, Hardware, Outcome
from etude.records import Tally
from etude.spec import Record, Until

MAX_OBSERVATION_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
_WRITER_RECORD = re.compile(r"metrics/[0-9a-f]{12}\.json")
_EVAL_BLOCK = re.compile(r"blocks/[0-9]{20}\.[0-9a-f]+/(?:block\.json|[A-Za-z0-9_]+\.parquet)")
_ARTIFACT = re.compile(r"artifacts/[0-9a-f]{64}\.json")


def member_folder(name: str) -> bool:
    parts = name.split("/")
    return len(parts) <= 2 and all(
        SAFE_NAME.fullmatch(part) and part not in {"out", "artifacts"} for part in parts
    )


def evaluation_file(key: str) -> bool:
    return key in ("run.json", "outcome.json", "tournament.json", "environment.json") or bool(
        _WRITER_RECORD.fullmatch(key) or _EVAL_BLOCK.fullmatch(key)
    )


def evidence_file(key: str) -> bool:
    """Portable evidence relative to a run root."""
    return evaluation_file(key) or bool(
        NAME.fullmatch(key)
        or _ARTIFACT.fullmatch(key)
        or re.fullmatch(r"checkpoints/[^./][^/]*\.json", key)
    )


def evidence_member(key: str) -> tuple[str, str]:
    """Split a group-relative key. The receiver checks membership against the root declaration."""
    if evidence_file(key):
        return "", key
    parts = key.split("/")
    for count in (1, 2):
        folder, name = "/".join(parts[:count]), "/".join(parts[count:])
        if member_folder(folder) and evaluation_file(name):
            return folder, name
    raise ValueError("invalid view evidence key")


def _origin(value: str) -> str:
    url = urlsplit(value)
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        any(c.isspace() for c in value)
        or url.port == 0
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or not (url.scheme == "https" or local and url.scheme == "http")
    ):
        raise ValueError("view URL must be an HTTPS origin or loopback HTTP origin")
    return value.rstrip("/")


ViewOrigin = Annotated[str, AfterValidator(_origin)]


class ViewGrant(Record):
    v: Literal[1] = 1
    origin: ViewOrigin
    run: str
    attempt: str
    attempt_number: int = Field(ge=1)
    exp: int

    def sign(self, secret: bytes) -> str:
        payload = base64.urlsafe_b64encode(self.model_dump_json().encode())
        return payload.decode() + "." + hmac.digest(secret, payload, "sha256").hex()

    @classmethod
    def verify(cls, token: str, secret: bytes, origin: str | None) -> "ViewGrant":
        try:
            payload, signature = token.split(".")
            expected = hmac.digest(secret, payload.encode("ascii"), "sha256").hex()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            grant = cls.model_validate_json(base64.urlsafe_b64decode(payload))
        except ValueError:
            raise EtudeError("invalid_view_grant", 401) from None
        if grant.exp <= time.time() or origin is not None and grant.origin != origin.rstrip("/"):
            raise EtudeError("view_grant_refused", 403)
        return grant


def launcher_origin() -> str | None:
    origin = os.environ.get("ETUDE_VIEW_URL")
    try:
        return _origin(origin) if origin else None
    except ValueError:
        raise LauncherError(
            "ETUDE_VIEW_URL must be an HTTPS origin or loopback HTTP origin"
        ) from None


class ViewSigner(Record):
    origin: ViewOrigin
    secret: bytes = Field(repr=False, exclude=True)

    @classmethod
    def configured(cls) -> Self | None:
        origin = launcher_origin()
        secret = os.environ.get("ETUDE_VIEW_SECRET")
        if origin and secret:
            return cls(origin=origin, secret=secret.encode())
        if origin or secret:
            raise LauncherError("set ETUDE_VIEW_URL and ETUDE_VIEW_SECRET together")
        return None


class ViewContext(Record):
    machine: str
    hardware: Hardware | None
    max_attempts: int
    until: Until | None
    source: str | None
    config: str | None


class ConsoleObservation(Record):
    position: int = Field(ge=0, le=2**63 - 1)
    body: str
    gaps: bool
    complete: bool


class AttemptObservation(Record):
    producer_seq: int = Field(ge=1, le=2**63 - 1)
    phase: Literal["setting_up", "running", "done"]
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    outcome: Outcome | None
    cause: Cause | None
    exit_code: int | None
    clocks: dict[str, float]
    tally: Tally | None
    context: ViewContext
    console: ConsoleObservation | None

    @model_validator(mode="after")
    def _check_phase(self) -> Self:
        done = self.phase == "done"
        if (self.finished_at is not None) != done or (self.outcome is not None) != done:
            raise ValueError("a finished attempt needs both its time and its outcome")
        if self.phase == "running" and self.started_at is None:
            raise ValueError("a running attempt needs its claim time")
        if self.console and self.console.complete and not done:
            raise ValueError("an active attempt cannot close its console")
        return self


class ViewGrantTarget(Record):
    origin: ViewOrigin
    token: str = Field(repr=False)
    context: ViewContext

    def place(self, session: Path, attempt: str) -> None:
        folder = session / ".view"
        folder.mkdir(mode=0o700, exist_ok=True)
        # NamedTemporaryFile creates the grant with mode 0600 before writing any bytes.
        with NamedTemporaryFile(dir=folder, delete=False) as staged:
            staged.write(self.model_dump_json().encode())
        Path(staged.name).replace(folder / (attempt + ".json"))
