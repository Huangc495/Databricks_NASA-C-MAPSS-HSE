"""Prepare immutable, checksum-verified FD001 inputs without cloud compute."""
import hashlib
import json
from pathlib import Path

from sentinelops.data import MD5, URL, download, read_trajectories


def prepare(source: Path, destination: Path) -> dict:
    download(source)  # Revalidates the cached archive before extracting.
    files = {}
    for split, expected in (("train", 20631), ("test", 13096)):
        name = f"{split}_FD001.txt"
        frame = read_trajectories(source / name)
        if len(frame) != expected or frame.unit.nunique() != 100:
            raise ValueError("Unexpected FD001 trajectory cardinality")
        files[f"trajectories/{name}"] = (source / name).read_bytes()
    values = (source / "RUL_FD001.txt").read_text().split()
    if len(values) != 100 or any(not v.isdigit() for v in values):
        raise ValueError("Expected 100 nonnegative official endpoint labels")
    files["labels/FD001.json"] = ("\n".join(json.dumps({
        "dataset": "CMAPSS", "subset": "FD001", "split": "test",
        "unit": i, "rul": int(value),
    }, sort_keys=True) for i, value in enumerate(values, 1)) + "\n").encode()
    manifest = {"source": URL, "archive_md5": MD5, "files": {}}
    for name, payload in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != payload:
            raise ValueError(f"Refusing to overwrite immutable landing file: {name}")
        path.write_bytes(payload)
        manifest["files"][name] = hashlib.sha256(payload).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    prepare(Path("data/cmapss"), Path("data/landing/v1"))
