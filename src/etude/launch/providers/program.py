"""Publish the client program and prepare rental bootstrap inputs.

Program owns package publication for one provider composition. Remote archive
unpacking and bootstrap stay independent of this controller-side module.
"""

from __future__ import annotations

import base64
import hashlib

from etude.launch.machines import MachineRequest
from etude.launch.providers.package import program_archive
from etude.launch.records import Image
from etude.launch.storage.s3 import Bucket
from etude.storage_timeout import storage_environment


class Program:
    def __init__(self, bucket: Bucket, prefix: str) -> None:
        self._bucket, self._prefix = bucket, prefix
        self._digest: str | None = None

    def prepare(self, request: MachineRequest, image: Image) -> dict[str, str]:
        """Publish once per composition; issue fresh links for each rental."""
        assert request.deadline is not None and request.bootstrap is not None
        timeouts = storage_environment()
        if self._digest is None:
            code = program_archive()
            digest = hashlib.sha256(code).hexdigest()
            key = f"{self._prefix}/programs/{digest}.zip"
            if self._bucket.get(key) is None:
                self._bucket.put(key, code, once=True)
            self._digest = digest
        return {
            **timeouts,
            "ETUDE_LOG_SECONDS": "" if request.log_seconds is None else str(request.log_seconds),
            "ETUDE_IMAGE": image.model_dump_json(),
            "ETUDE_SESSION": request.session,
            "ETUDE_STARTUP_DEADLINE": request.startup_deadline.isoformat()
            if request.startup_deadline
            else "",
            "ETUDE_DEADLINE": request.deadline.isoformat(),
            "ETUDE_MAX_USD": str(request.cap_usd),
            "ETUDE_BOOTSTRAP_URL": request.bootstrap.url,
            "ETUDE_BOOTSTRAP_LOG_URL": request.bootstrap.log_url,
            "ETUDE_WORKER_LOG_URL": request.bootstrap.worker_log_url,
            "ETUDE_PACKAGE_URL": self._bucket.get_url(
                f"{self._prefix}/programs/{self._digest}.zip", 13 * 3600
            ),
            "ETUDE_PACKAGE_SHA256": self._digest,
            "ETUDE_PROGRAM": base64.b64encode(program_archive(guardian=True)).decode(),
        }


def boot_source(module: str) -> str:
    """Bare-Python entrypoint that unpacks the guardian before importing it."""
    return f"""import base64,io,os,pathlib,sys,zipfile
root=pathlib.Path('/opt/etude/src')
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(io.BytesIO(base64.b64decode(os.environ.pop('ETUDE_PROGRAM')))) as z:
    z.extractall(root)
sys.path.insert(0,str(root))
from {module} import main
raise SystemExit(main(['rental','boot']))
"""
