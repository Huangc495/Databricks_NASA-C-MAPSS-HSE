import hashlib
import json

import pytest

from sentinelops import landing


def test_verified_landing_preserves_raw_and_endpoint_order(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    # Isolate archive validation from the small cardinality-independent test.
    def extract(_):
        for split in ("train", "test"):
            (source / f"{split}_FD001.txt").write_text("1 1 " + "0 " * 24 + "\n")
        (source / "RUL_FD001.txt").write_text("\n".join(str(i) for i in range(100)))
    class Frame:
        class Unit:
            def nunique(self): return 100
        unit = Unit()
        def __init__(self, n): self.n = n
        def __len__(self): return self.n
    monkeypatch.setattr(landing, "download", extract)
    monkeypatch.setattr(landing, "read_trajectories", lambda p: Frame(20631 if p.name.startswith("train") else 13096))
    destination = tmp_path / "landing"
    manifest = landing.prepare(source, destination)
    labels = [json.loads(line) for line in (destination / "labels/FD001.json").read_text().splitlines()]
    assert labels[0]["unit"] == 1 and labels[0]["rul"] == 0
    assert labels[-1]["unit"] == 100 and labels[-1]["rul"] == 99
    assert labels[0]["split"] == "test"
    raw = destination / "trajectories/train_FD001.txt"
    assert raw.read_bytes() == (source / "train_FD001.txt").read_bytes()
    assert manifest["files"]["trajectories/train_FD001.txt"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert landing.prepare(source, destination) == manifest
    raw.write_text("corrupted")
    with pytest.raises(ValueError, match="immutable"):
        landing.prepare(source, destination)


def test_cached_archive_must_pass_checksum(tmp_path):
    (tmp_path / "CMAPSSData.zip").write_bytes(b"not the NASA archive")
    with pytest.raises(ValueError, match="checksum"):
        landing.prepare(tmp_path, tmp_path / "output")
