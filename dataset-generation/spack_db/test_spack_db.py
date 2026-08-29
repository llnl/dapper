# test_spack_db.py
import json
import os
import io
import tarfile
import pytest
import spack_db as pl
from requests import exceptions as req_exc


def test_update_index_entry_sets_paths(tmp_path):
    idx = {}
    pkg_hash = "deadbeef"
    value = {"spec": {"name": "foo", "version": "1.2.3"}}
    tar_hash = "abc123"
    pl.update_index_entry(idx, pkg_hash, value, tar_hash)
    assert pkg_hash in idx
    entry = idx[pkg_hash]
    assert entry["name"] == "foo"
    assert entry["version"] == "1.2.3"
    assert entry["sha256"] == "abc123"
    assert entry["manifest_path"].endswith(f"manifest/foo-1.2.3-deadbeef.json")
    assert entry["tarinfo_path"].endswith(f"tarinfo/abc123.json")


def test_index_save_and_backup(tmp_path):
    pl.save_index({"a": 1})
    # First save: no .bak yet
    assert os.path.exists(pl.INDEX_FILE)
    assert not os.path.exists(pl.INDEX_FILE + ".bak")

    # Second save should write a .bak of previous
    pl.save_index({"a": 2})
    assert os.path.exists(pl.INDEX_FILE + ".bak")
    with open(pl.INDEX_FILE) as f:
        assert json.load(f)["a"] == 2
    with open(pl.INDEX_FILE + ".bak") as f:
        assert json.load(f)["a"] == 1


def test_checkpoint_roundtrip(tmp_path):
    pl.save_checkpoint("pkg-hash-123")
    assert pl.load_checkpoint() == "pkg-hash-123"


def test_remove_lines_spec_manifest_and_extract_hash(sample_manifest_json):
    cleaned = pl.remove_lines_spec_manifest(sample_manifest_json)
    # ensure we got dict and our access function finds the right tarball hash
    assert isinstance(cleaned, dict)
    h = pl.access_spec_manifest_media_type(cleaned)
    assert h == "f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351"

def test_malformed_manifest_is_logged(fake_requests, tmp_path):
    # Set up a fake package hash and minimal database entry
    pkg_hash = "badbadbadbadbadbadbadbadbadbadba"
    name = "broken-pkg"
    ver = "1.0"
    db_entry = {"spec": {"name": name, "version": ver}}
    
    # Manifest URL that print_files will request
    manifest_url = f"https://binaries.spack.io/develop/v3/manifests/spec/{name}/{name}-{ver}-{pkg_hash}.spec.manifest.json"
    
    # Make fake_requests return invalid bytes (invalid JSON)
    fake_requests.table[manifest_url] = lambda: type(
        "R", (), {"status_code": 200, "content": b"xxxx"}  # bad data
    )()
    
    # Run print_files, which should try to parse the manifest and fail
    index = {}
    existing_tarinfo_files = set()
    seen_tarball_hashes = set()
    pl.print_files(pkg_hash, db_entry, index, existing_tarinfo_files, seen_tarball_hashes)
    
    # Check: malformed_manifests.txt exists and has the hash + URL + error
    assert os.path.exists(pl.MALFORMED_MANIFESTS_FILE)
    with open(pl.MALFORMED_MANIFESTS_FILE, "r") as f:
        log_content = f.read()
    assert pkg_hash in log_content
    assert manifest_url in log_content
    assert "ValueError" in log_content or "JSON" in log_content 
    
    # Check: skipped_manifests.txt
    assert os.path.exists(pl.SKIPPED_MANIFESTS_FILE)
    with open(pl.SKIPPED_MANIFESTS_FILE, "r") as f:
        skipped_content = f.read()
    assert pkg_hash in skipped_content

def test_make_urls():
    tar_hash = "f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351"
    url = pl.make_binary_package_URL(tar_hash)

    # Basic correctness
    assert url.endswith("/" + tar_hash)
    assert f"/{tar_hash[:2]}/" in url  # path uses first two hex chars as subdir

    # manifest URL
    pkg_hash = "bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
    pkg_val = {"spec": {"name": "compiler-wrapper", "version": "1.0"}}
    murl, package_filename = pl.make_spec_manifest_URL(pkg_hash, pkg_val)
    assert "manifests/spec/compiler-wrapper/" in murl
    assert murl.endswith(f"compiler-wrapper-1.0-{pkg_hash}.spec.manifest.json")
    assert package_filename == f"compiler-wrapper/compiler-wrapper-1.0-{pkg_hash}"



def test_download_from_URL_spec_does_not_persist(fake_requests, tmp_path):
    # For is_spec=True, function returns bytes but should NOT save a file to SPEC_CACHE_DIR
    url = "https://example.com/x.spec.manifest.json"
    content = b"hello"
    fake_requests.table[url] = lambda: type("R", (), {"status_code": 200, "content": content})()

    out = pl.download_from_URL(url, "compiler/x-1.0-abc", is_spec=True)
    # Returned content
    assert out == content
    # Not persisted
    cached_path = os.path.join(pl.SPEC_CACHE_DIR, "compiler__x-1.0-abc")
    assert not os.path.exists(cached_path)


def test_download_from_URL_binary_persists(fake_requests, tmp_path):
    url = "https://example.com/blob.tar.gz"
    content = b"tarbytes"
    fake_requests.table[url] = lambda: type("R", (), {"status_code": 200, "content": content})()

    out = pl.download_from_URL(url, "compiler/x-1.0-abc", is_spec=False)
    assert out == content
    cached_path = os.path.join(pl.BINARY_CACHE_DIR, "compiler__x-1.0-abc")
    assert os.path.exists(cached_path)
    with open(cached_path, "rb") as f:
        assert f.read() == content


def test_download_timeout_logs(fake_requests, tmp_path):
    url = "https://timeout.test/blob.tar.gz"
    fake_requests.table[url] = lambda: req_exc.Timeout()

    out = pl.download_from_URL(url, "compiler/x-1.0-abc", is_spec=False)
    assert out is None
    with open(pl.TIMEOUT_LOG_FILE, "r") as f:
        txt = f.read()
    assert "compiler__x-1.0-abc" in txt
    assert url in txt


def test_read_binary_package_extracts_and_cleans_paths(tar_with_placeholder_bytes, tmp_path):
    # Prepare the "downloaded" tar path that read_binary_package will delete afterward
    pkg = "compiler-wrapper/compiler-wrapper-1.0-bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
    tar_hash = "f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351"
    dl_path = os.path.join(pl.BINARY_CACHE_DIR, pkg.replace("/", "__"))
    with open(dl_path, "wb") as f:
        f.write(b"placeholder")

    # Run
    pl.read_binary_package(tar_with_placeholder_bytes, pkg, tar_hash)

    # Verify cleaned tarinfo file written atomically
    tarinfo_path = os.path.join(pl.TARINFO_DIR, f"{tar_hash}.json")
    assert os.path.exists(tarinfo_path)
    with open(tarinfo_path) as f:
        items = json.load(f)
    # Path should start *after* the 'pkg-tail/' segment due to remove_placeholder_directories
    assert any(p.endswith(".spack/install_environment.json") for p in items)

    # tarball removed and buffer freed
    assert not os.path.exists(dl_path)


def test_print_files_happy_path(fake_requests, sample_manifest_json, tar_with_placeholder_bytes, tmp_path):
    # minimal database with one install
    pkg_hash = "bsavlbvtqsc7yjtvka3ko3aem4wye2u3"
    db = {
        "database": {
            "installs": {
                pkg_hash: {"spec": {"name": "compiler-wrapper", "version": "1.0"}}
            }
        }
    }
    index = {}
    existing_tarinfo_files = set()  # no prior tarinfo
    seen_tarball_hashes = set()

    # Wire URLs expected by print_files flow
    manifest_url = (
        "https://binaries.spack.io/develop/v3/manifests/spec/"
        f"compiler-wrapper/compiler-wrapper-1.0-{pkg_hash}.spec.manifest.json"
    )
    tar_hash = "f4d1969c7a82c76b962ae969c91d7b54cc11e0ce9f1ec9277789990f58aab351"
    blob_url = f"https://binaries.spack.io/develop/blobs/sha256/{tar_hash[:2]}/{tar_hash}"

    fake_requests.table[manifest_url] = lambda: type("R", (), {"status_code": 200, "content": sample_manifest_json})()
    fake_requests.table[blob_url] = lambda: type("R", (), {"status_code": 200, "content": tar_with_placeholder_bytes})()

    # Act
    pl.print_files(pkg_hash, db["database"]["installs"][pkg_hash], index, existing_tarinfo_files, seen_tarball_hashes)

    # Assert manifest saved safely
    manifest_path = os.path.join(pl.MANIFEST_DIR, f"compiler-wrapper-1.0-{pkg_hash}.json")
    assert os.path.exists(manifest_path)
    # Assert tarinfo created
    tarinfo_path = os.path.join(pl.TARINFO_DIR, f"{tar_hash}.json")
    assert os.path.exists(tarinfo_path)
    # Index updated & saved
    assert pkg_hash in index
    assert index[pkg_hash]["sha256"] == tar_hash
    assert os.path.exists(pl.INDEX_FILE)


def test_print_files_skips_when_tarinfo_exists(sample_manifest_json, tmp_path):
    # Prepare: existing tarinfo means no binary download
    pkg_hash = "zzz111"
    spec = {"spec": {"name": "foo", "version": "9.9"}}
    index = {}

    # Pre-create tarinfo
    prehash = "abcdead00face"
    existing_tarinfo_files = {prehash}
    seen_tarball_hashes = set()

    # Monkeypatch access_spec_manifest_media_type to return prehash so it matches existing_tarinfo_files
    orig = pl.access_spec_manifest_media_type
    pl.access_spec_manifest_media_type = lambda _db: prehash

    # Also ensure manifest path is considered existing so we don't try to download it
    manifest_path = os.path.join(pl.MANIFEST_DIR, f"foo-9.9-{pkg_hash}.json")
    with open(manifest_path, "w") as f:
        json.dump({"dummy": True}, f)

    try:
        pl.print_files(pkg_hash, spec, index, existing_tarinfo_files, seen_tarball_hashes)
    finally:
        pl.access_spec_manifest_media_type = orig

    # Should just update index and save, no download attempted
    assert pkg_hash in index
    assert index[pkg_hash]["sha256"] == prehash
    with open(pl.INDEX_FILE) as f:
        data = json.load(f)
    assert pkg_hash in data


def test_download_404_returns_none(fake_requests):
    url = "https://example.com/notfound"
    fake_requests.table[url] = lambda: type("R", (), {"status_code": 404, "content": b""})()
    assert pl.download_from_URL(url, "some/pkg-1.0-deadbeef", is_spec=True) is None

def _mk_manifest_bytes_with_hash(tar_hash: str):
    """Helper: build bytes that match remove_lines_spec_manifest's slicing contract."""
    body = {"data": [
        {"mediaType": "x/ignored", "checksum": "zzz"},
        {"mediaType": "application/vnd.spack.install.v2.tar+gzip", "checksum": tar_hash},
    ]}
    raw = json.dumps(body).encode("utf-8")
    return b"x" * 49 + raw + b"y" * 834


def test_shared_tarball_logs_and_skips_second_download(fake_requests, tmp_path):
    """
    Two different package hashes point to the same tarball hash.
    We expect:
      - First run downloads tarball and writes tarinfo.
      - Second run logs to SHARED_TARBALL_HASH_FILE and (since we update
        existing_tarinfo_files to include the first tarinfo) skips re-download.
    """
    # Common tarball hash & blob URL
    tar_hash = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
    blob_url = f"https://binaries.spack.io/develop/blobs/sha256/{tar_hash[:2]}/{tar_hash}"

    # Package A
    pkg_hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    name = "compiler-wrapper"
    ver = "1.0"
    man_url_a = f"https://binaries.spack.io/develop/v3/manifests/spec/{name}/{name}-{ver}-{pkg_hash_a}.spec.manifest.json"

    # Package B (different spec hash, same tarball)
    pkg_hash_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    man_url_b = f"https://binaries.spack.io/develop/v3/manifests/spec/{name}/{name}-{ver}-{pkg_hash_b}.spec.manifest.json"

    # Route manifests to same tarball hash, and the blob to some tar bytes
    tar_bytes = b"\x1f\x8b" + b"tar" * 100  # not actually parsed; we won't open it here
    # For the first call we want a real tar.gz
    # We'll just reuse download + skip path by creating a minimal valid tar.gz:
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name=f"{name}-{ver}-{pkg_hash_a}/.spack/install_environment.json")
        data = b"{}"
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    tar_bytes = buf.getvalue()

    fake_requests.table[man_url_a] = lambda: type("R", (), {"status_code": 200, "content": _mk_manifest_bytes_with_hash(tar_hash)})()
    fake_requests.table[man_url_b] = lambda: type("R", (), {"status_code": 200, "content": _mk_manifest_bytes_with_hash(tar_hash)})()
    fake_requests.table[blob_url]   = lambda: type("R", (), {"status_code": 200, "content": tar_bytes})()

    index = {}
    existing_tarinfo_files = set()
    seen_tarball_hashes = set()

    # Run A (creates tarinfo and index)
    pl.print_files(pkg_hash_a, {"spec": {"name": name, "version": ver}}, index, existing_tarinfo_files, seen_tarball_hashes)
    tarinfo_path = os.path.join(pl.TARINFO_DIR, f"{tar_hash}.json")
    assert os.path.exists(tarinfo_path)

    # Emulate the main loop behavior: keep using *the same* existing_tarinfo_files set,
    # but update it to reflect that we've now created tarinfo.
    existing_tarinfo_files.add(tar_hash)

    # Guard: if the second call tries to re-download, we'd need another blob mapping.
    # We purposely *don't* add one here—so if it tries, the test will fail.

    # Run B (should log as shared and skip binary download due to existing_tarinfo_files)
    pl.print_files(pkg_hash_b, {"spec": {"name": name, "version": ver}}, index, existing_tarinfo_files, seen_tarball_hashes)

    # Check shared log file captured the second spec hash + the shared tar hash
    assert os.path.exists(pl.SHARED_TARBALL_HASH_FILE)
    with open(pl.SHARED_TARBALL_HASH_FILE, "r") as f:
        shared_log = f.read()
    assert f"{pkg_hash_b}\t{tar_hash}" in shared_log

    # Index should contain both manifests, same sha256
    assert index[pkg_hash_a]["sha256"] == tar_hash
    assert index[pkg_hash_b]["sha256"] == tar_hash


def test_failed_tarball_download_is_logged(fake_requests, tmp_path):
    """
    If the blob download fails (404 or None), we should append to FAILED_TARBALL_DOWNLOAD_FILE
    and not produce a tarinfo file.
    """
    name = "foo"
    ver = "9.9"
    pkg_hash = "cccccccccccccccccccccccccccccccc"
    tar_hash = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    man_url = f"https://binaries.spack.io/develop/v3/manifests/spec/{name}/{name}-{ver}-{pkg_hash}.spec.manifest.json"
    blob_url = f"https://binaries.spack.io/develop/blobs/sha256/{tar_hash[:2]}/{tar_hash}"

    # Manifest OK, blob 404
    fake_requests.table[man_url] = lambda: type("R", (), {"status_code": 200, "content": _mk_manifest_bytes_with_hash(tar_hash)})()
    fake_requests.table[blob_url] = lambda: type("R", (), {"status_code": 404, "content": b""})()

    index = {}
    existing_tarinfo_files = set()
    seen_tarball_hashes = set()

    pl.print_files(pkg_hash, {"spec": {"name": name, "version": ver}}, index, existing_tarinfo_files, seen_tarball_hashes)

    # No tarinfo created
    tarinfo_path = os.path.join(pl.TARINFO_DIR, f"{tar_hash}.json")
    assert not os.path.exists(tarinfo_path)

    # Log entry created with package filename, manifest hash, and tarball hash
    assert os.path.exists(pl.FAILED_TARBALL_DOWNLOAD_FILE)
    with open(pl.FAILED_TARBALL_DOWNLOAD_FILE, "r") as f:
        log = f.read()
    # Contains the manifest hash + tarball hash; also includes the package_filename prefix
    assert f"manifest hash: {pkg_hash}, tarball hash: {tar_hash}" in log
