"""Upload a prepared landing directory to a Unity Catalog volume, never overwriting (used by CI).

Landing is immutable: a file already in the volume must be byte-identical to the local one (it is
then skipped), and a new file is sent with the Files API's overwrite=False, so it appears whole or
not at all. Prints one JSON summary line.

  python -m sentinelops.landing   # checksum-verified NASA archive -> data/landing/v1
  python scripts/upload_landing.py data/landing/v1 /Volumes/sentinelops_staging/sentinelops/landing/cmapss_ingest/v1
"""
import hashlib
import io
import json
from pathlib import Path
import sys


def upload(client, local: Path, remote: str) -> dict:
    from databricks.sdk.errors import NotFound

    summary = {"uploaded": [], "identical": [], "bytes": 0}
    for path in sorted(p for p in local.rglob("*") if p.is_file()):
        relative = path.relative_to(local).as_posix()
        target, data = f"{remote.rstrip('/')}/{relative}", path.read_bytes()
        try:
            client.files.get_metadata(target)
        except NotFound:
            client.files.create_directory(target.rsplit("/", 1)[0])
            client.files.upload(target, io.BytesIO(data), overwrite=False)
            summary["uploaded"].append(relative)
            summary["bytes"] += len(data)
            continue
        landed = client.files.download(target).contents.read()
        if hashlib.sha256(landed).digest() != hashlib.sha256(data).digest():
            raise ValueError(f"Refusing to overwrite immutable landing file {target}: its content differs")
        summary["identical"].append(relative)
    return summary


if __name__ == "__main__":
    from databricks.sdk import WorkspaceClient

    local, remote = Path(sys.argv[1]), sys.argv[2]
    if not remote.startswith("/Volumes/") or not local.is_dir():
        raise SystemExit("Usage: upload_landing.py <local directory> /Volumes/<catalog>/<schema>/<volume>/<path>")
    result = upload(WorkspaceClient(), local, remote)
    print(json.dumps({"remote": remote, "uploaded": len(result["uploaded"]), "identical": len(result["identical"]),
                      "bytes": result["bytes"]}))
