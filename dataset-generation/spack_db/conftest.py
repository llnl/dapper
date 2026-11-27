# conftest.py
import io
import json
import tarfile
import types
import pytest


import spack_db as pl

@pytest.fixture(autouse=True)
def isolate_fs(tmp_path, monkeypatch):
    """Redirect all cache/config paths to a temp dir per test."""
    cache = tmp_path / "cache"
    (cache / "manifest").mkdir(parents=True, exist_ok=True)
    (cache / "tarinfo").mkdir(parents=True, exist_ok=True)
    (cache / "spec_manifests").mkdir(parents=True, exist_ok=True)
    (cache / "binary_packages").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pl, "INDEX_FILE", str(cache / "spack.index.db.json"), raising=False)
    monkeypatch.setattr(pl, "MANIFEST_DIR", str(cache / "manifest"), raising=False)
    monkeypatch.setattr(pl, "TARINFO_DIR", str(cache / "tarinfo"), raising=False)
    monkeypatch.setattr(pl, "SPEC_CACHE_DIR", str(cache / "spec_manifests"), raising=False)
    monkeypatch.setattr(pl, "BINARY_CACHE_DIR", str(cache / "binary_packages"), raising=False)

    monkeypatch.setattr(pl, "CHECKPOINT_FILE", str(tmp_path / "progress.txt"), raising=False)
    monkeypatch.setattr(pl, "SKIPPED_MANIFESTS_FILE", str(cache / "skipped_manifests.txt"), raising=False)
    monkeypatch.setattr(pl, "MALFORMED_MANIFESTS_FILE", str(cache / "malformed_manifests.txt"), raising=False)
    monkeypatch.setattr(pl, "TIMEOUT_LOG_FILE", str(cache / "timeouts.txt"), raising=False)
    monkeypatch.setattr(pl, "MISSING_TARBALL_HASH_FILE", str(cache / "missing_tarballs.txt"), raising=False)
    monkeypatch.setattr(pl, "SHARED_TARBALL_HASH_FILE", str(cache / "shared_tarballs.txt"), raising=False)
    monkeypatch.setattr(pl, "FAILED_TARBALL_DOWNLOAD_FILE", str(cache / "failed_tarball_downloads.txt"), raising=False)

    # Ensure directories exist for atomic writes
    (tmp_path / "cache").mkdir(exist_ok=True)
    yield


@pytest.fixture
def sample_manifest_json():
    """
    Create the *actual bytes* expected by remove_lines_spec_manifest:
    take a valid JSON, then pad 49 bytes in front and 834 bytes at the end.
    """
    body = {
        "data": [
            {"mediaType": "irrelevant/type", "checksum": "abc"},
            {"mediaType": "application/vnd.spack.install.v2.tar+gzip",
             "checksum": "f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351"}
        ]
    }
    raw = json.dumps(body).encode("utf-8")
    return b"x" * 49 + raw + b"y" * 834


@pytest.fixture
def tar_with_placeholder_bytes():
    """
    Build a tar in-memory whose members include the __spack_path_placeh segments
    and the package-tail folder (e.g., 'compiler-wrapper-1.0-<hash>').
    """
    pkg_tail = "compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
    member_name = (
        "home/software/spack/__spack_path_placeholder__/__spack_path_placeholder__/"
        "__spack_path_placeholder__/__spack_path_placeh/morepadding/linux-x86_64_v3/"
        f"{pkg_tail}/.spack/install_environment.json"
    )

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"{}"
        tarinfo = tarfile.TarInfo(name=member_name)
        tarinfo.size = len(data)
        tf.addfile(tarinfo, io.BytesIO(data))
    return buf.getvalue()


class DummyResp:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


@pytest.fixture
def fake_requests(monkeypatch):
    """
    Monkeypatch requests.get with programmable behavior per-URL.
    Usage:
        table = {}
        def _route(url, *a, **kw): return table[url]()
        fake = fake_requests
        fake.route = _route
        monkeypatch.setattr(pl.requests, "get", _route)
        table["...json"] = lambda: DummyResp(200, b"...")
    """
    table = {}

    def _get(url, *args, **kwargs):
        if url not in table:
            raise AssertionError(f"Unexpected URL requested: {url}")
        result = table[url]()
        # Allow raising exceptions (e.g., Timeout) from factories
        if isinstance(result, Exception):
            raise result
        return result

    # Expose for tests to fill
    _get.table = table
    monkeypatch.setattr(pl.requests, "get", _get)
    return _get