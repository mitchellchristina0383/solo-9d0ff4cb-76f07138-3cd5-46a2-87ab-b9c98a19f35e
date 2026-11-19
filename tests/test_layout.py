"""Tests for the OCI image layout repacker (`app.layout` / `POST /repack`)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.layout import LayoutError, repack_layout_tar
from app.main import app
from tests.oci_helpers import (
    OCI_INDEX,
    OCI_LAYER_GZIP,
    OCI_LAYER_TAR,
    OCI_MANIFEST,
    REF_NAME,
    LayoutBuilder,
    _read_tar,
    _tar,
    import_image,
    layer_tar,
    list_index,
)

client = TestClient(app)

SHARED = layer_tar([("dir", "shared/"), ("file", "shared/base", b"shared-base\n")])
COMMON = layer_tar([("file", "etc/", b"")])  # placeholder replaced below
AMD_ONLY = layer_tar([("file", "arch/amd64", b"amd\n")])
ARM_ONLY = layer_tar([("file", "arch/arm64", b"arm\n")])
GZIPPED = gzip.compress(layer_tar([("file", "already/gz", b"kept\n")]), mtime=0)


def build_shared_layout() -> bytes:
    builder = LayoutBuilder()
    builder.add_image(
        architecture="amd64",
        tag="repo:latest",
        layers=[
            (OCI_LAYER_TAR, SHARED),
            (OCI_LAYER_TAR, AMD_ONLY),
            (OCI_LAYER_GZIP, GZIPPED),
        ],
    )
    builder.add_image(
        architecture="arm64",
        variant="v8",
        tag="repo:latest",
        layers=[
            (OCI_LAYER_TAR, SHARED),  # shared with amd64
            (OCI_LAYER_TAR, ARM_ONLY),
            (OCI_LAYER_GZIP, GZIPPED),  # already gzip: kept as is
        ],
    )
    return builder.build(nested_index=True, top_annotations={"x.test/note": "keep"})


def repack(tar_bytes: bytes) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "layout.tar")
        with open(src, "wb") as fh:
            fh.write(tar_bytes)
        result = repack_layout_tar(src, tmp)
        with open(result.output_path, "rb") as fh:
            files = _read_tar(fh.read())
        return {
            "files": files,
            "result": result,
            "index": json.loads(files["index.json"]),
            "layout": json.loads(files["oci-layout"]),
        }


def repack_expect_error(tar_bytes: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "layout.tar")
        with open(src, "wb") as fh:
            fh.write(tar_bytes)
        with pytest.raises(LayoutError) as excinfo:
            repack_layout_tar(src, tmp)
        return str(excinfo.value)


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_repack_flattens_index_and_preserves_platforms_and_tags():
    out = repack(build_shared_layout())
    index = out["index"]
    assert index["schemaVersion"] == 2
    assert index["mediaType"] == OCI_INDEX
    assert index["annotations"] == {"x.test/note": "keep"}
    assert len(index["manifests"]) == 2

    by_arch = {}
    for desc in index["manifests"]:
        assert desc["mediaType"] == OCI_MANIFEST
        assert "index" not in desc["mediaType"]
        assert desc["platform"]["os"] == "linux"
        assert desc["annotations"][REF_NAME] == "repo:latest"
        by_arch[desc["platform"]["architecture"]] = desc
    assert set(by_arch) == {"amd64", "arm64"}
    assert by_arch["arm64"]["platform"]["variant"] == "v8"

    # The nested index must not survive in the flattened output.
    for name, raw in out["files"].items():
        if not name.startswith("blobs/"):
            continue
        try:
            blob = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue  # gzip layer
        assert blob.get("mediaType") != OCI_INDEX


def test_layer_media_types_sizes_and_digests_are_updated():
    out = repack(build_shared_layout())
    files = out["files"]

    for desc in out["index"]["manifests"]:
        manifest = json.loads(files["blobs/sha256/" + desc["digest"].split(":")[1]])
        for layer in manifest["layers"]:
            raw = files["blobs/sha256/" + layer["digest"].split(":")[1]]
            assert len(raw) == layer["size"]
            assert hashlib.sha256(raw).hexdigest() == layer["digest"].split(":")[1]
            assert layer["mediaType"].endswith("+gzip")

    # Original uncompressed layer blobs are gone, gzip layer kept verbatim.
    result = out["result"]
    assert result.compressed_layers == 3  # SHARED, AMD_ONLY, ARM_ONLY
    assert result.reused_shared_layers == 1
    assert result.kept_gzip_layers == 2
    assert result.image_manifests == 2
    # 2 configs + 2 rewritten manifests + 3 freshly gzipped + 1 kept gzip blob
    assert result.output_blobs == 8
    actual_blobs = {
        n.split("/")[-1] for n in files if n.startswith("blobs/sha256/")
    }
    assert len(actual_blobs) == 8

    # Uncompressed source blobs are gone; the already-gzip blob is untouched.
    assert hashlib.sha256(SHARED).hexdigest() not in actual_blobs
    assert hashlib.sha256(AMD_ONLY).hexdigest() not in actual_blobs
    assert hashlib.sha256(ARM_ONLY).hexdigest() not in actual_blobs
    assert hashlib.sha256(GZIPPED).hexdigest() in actual_blobs
    assert files["blobs/sha256/" + hashlib.sha256(GZIPPED).hexdigest()] == GZIPPED


def test_shared_layer_is_compressed_once_and_decompresses_byte_exact():
    out = repack(build_shared_layout())
    files = out["files"]
    index = out["index"]

    manifests = [
        json.loads(files["blobs/sha256/" + d["digest"].split(":")[1]])
        for d in index["manifests"]
    ]
    first_layers = {m["layers"][0]["digest"] for m in manifests}
    assert len(first_layers) == 1, "shared layer must map to one gzip blob"
    shared_digest = next(iter(first_layers))
    shared_gz = files["blobs/sha256/" + shared_digest.split(":")[1]]
    assert gzip.decompress(shared_gz) == SHARED

    # Every gzip layer that was converted round-trips exactly to its source.
    sources = {SHARED, AMD_ONLY, ARM_ONLY}
    converted = set()
    for manifest in manifests:
        for layer in manifest["layers"]:
            raw = files["blobs/sha256/" + layer["digest"].split(":")[1]]
            decompressed = gzip.decompress(raw)
            if decompressed in sources:
                converted.add(decompressed)
            else:
                # The pre-existing gzip layer must survive byte-for-byte.
                assert raw == GZIPPED
                assert decompressed == gzip.decompress(GZIPPED)
    assert converted == sources


def test_repacked_layout_imports_each_platform():
    original = build_shared_layout()
    out = repack(original)

    for arch, extra in (("amd64", AMD_ONLY), ("arm64", ARM_ONLY)):
        manifest, snap = import_image(
            out["files"],
            platform={"architecture": arch, "os": "linux"},
            tag="repo:latest",
        )
        assert "shared/base" in snap
        assert snap["shared/base"] == ("file", b"shared-base\n")
        assert "already/gz" in snap

    # Compare with the reference application of the original layout.
    src_files = _read_tar(original)
    _, amd_src = import_image(
        src_files, platform={"architecture": "amd64", "os": "linux"}, tag="repo:latest"
    )
    _, amd_new = import_image(
        out["files"], platform={"architecture": "amd64", "os": "linux"}, tag="repo:latest"
    )
    assert amd_src == amd_new

    _, arm_src = import_image(
        src_files, platform={"architecture": "arm64", "os": "linux"}, tag="repo:latest"
    )
    _, arm_new = import_image(
        out["files"], platform={"architecture": "arm64", "os": "linux"}, tag="repo:latest"
    )
    assert arm_src == arm_new


def test_flattened_layout_without_nested_index():
    builder = LayoutBuilder()
    builder.add_image(
        architecture="amd64", tag="a:1", layers=[(OCI_LAYER_TAR, SHARED)]
    )
    builder.add_image(
        architecture="arm64", tag="a:1", layers=[(OCI_LAYER_TAR, ARM_ONLY)]
    )
    out = repack(builder.build(nested_index=False))
    assert len(out["index"]["manifests"]) == 2
    for desc in out["index"]["manifests"]:
        assert desc["mediaType"] == OCI_MANIFEST
    _, snap = import_image(
        out["files"], platform={"architecture": "arm64", "os": "linux"}, tag="a:1"
    )
    assert "arch/arm64" in snap


def test_platform_inferred_from_config_when_absent():
    # A manifest descriptor without a platform: the config must supply it.
    builder = LayoutBuilder()
    builder.add_image(architecture="s390x", layers=[(OCI_LAYER_TAR, AMD_ONLY)])
    original = builder.build(nested_index=True)
    files = _read_tar(original)
    inner = json.loads(files["index.json"])["manifests"][0]
    inner_raw = json.loads(files["blobs/sha256/" + inner["digest"].split(":")[1]])
    del inner_raw["manifests"][0]["platform"]
    new_inner = json.dumps(inner_raw, sort_keys=True, separators=(",", ":")).encode()
    files["blobs/sha256/" + hashlib.sha256(new_inner).hexdigest()] = new_inner
    top = json.loads(files["index.json"])
    top["manifests"] = [
        {
            "mediaType": OCI_INDEX,
            "digest": "sha256:" + hashlib.sha256(new_inner).hexdigest(),
            "size": len(new_inner),
        }
    ]
    files["index.json"] = json.dumps(top).encode()

    out = repack(_tar(files))
    assert out["index"]["manifests"][0]["platform"] == {
        "architecture": "s390x",
        "os": "linux",
    }


def test_unreferenced_original_layers_are_dropped():
    builder = LayoutBuilder()
    builder.add_image(architecture="amd64", layers=[(OCI_LAYER_TAR, SHARED)])
    original = builder.build(nested_index=True)
    files = _read_tar(original)
    junk = b"orphan blob, never referenced"
    files["blobs/sha256/" + hashlib.sha256(junk).hexdigest()] = junk
    files["blobs/sha256/" + "0" * 64] = b"also orphan"

    out = repack(_tar(files))
    actual = {
        n.split("/")[-1] for n in out["files"] if n.startswith("blobs/sha256/")
    }
    # Every blob in the archive must be reachable from the new index.
    reachable = set()
    for desc in out["index"]["manifests"]:
        reachable.add(desc["digest"].split(":")[1])
        manifest = json.loads(
            out["files"]["blobs/sha256/" + desc["digest"].split(":")[1]]
        )
        reachable.add(manifest["config"]["digest"].split(":")[1])
        for layer in manifest["layers"]:
            reachable.add(layer["digest"].split(":")[1])
    assert actual == reachable
    assert len(actual) == out["result"].output_blobs


def test_config_blobs_are_preserved_byte_for_byte():
    original = _read_tar(build_shared_layout())
    out = repack(build_shared_layout())
    files = out["files"]

    config_digests = set()
    for desc in out["index"]["manifests"]:
        manifest = json.loads(files["blobs/sha256/" + desc["digest"].split(":")[1]])
        config = manifest["config"]
        config_digests.add(config["digest"].split(":")[1])
        # Byte-identical to the source config, and rootfs.diff_ids still
        # describe the *uncompressed* layer content (unchanged by gzip).
        name = "blobs/sha256/" + config["digest"].split(":")[1]
        assert files[name] == original[name]
        config_json = json.loads(files[name])
        assert len(config_json["rootfs"]["diff_ids"]) == len(manifest["layers"])

    assert len(config_digests) == 2


def test_output_archive_contains_only_expected_entries():
    out = repack(build_shared_layout())
    names = set(out["files"])
    assert "oci-layout" in names
    assert "index.json" in names
    assert all(
        n in ("oci-layout", "index.json")
        or n.startswith("blobs/sha256/")
        for n in names
    )
    assert out["layout"] == {"imageLayoutVersion": "1.0.0"}


# --------------------------------------------------------------------------- #
# failure modes: no artifact, clear error
# --------------------------------------------------------------------------- #
def _mutate_layout(mutator) -> bytes:
    files = _read_tar(build_shared_layout())
    mutator(files)
    return _tar(files)


def test_missing_blob_rejected():
    def drop(files):
        victim = next(n for n in files if n.startswith("blobs/sha256/"))
        del files[victim]

    error = repack_expect_error(_mutate_layout(drop))
    assert "missing" in error


def test_digest_mismatch_rejected():
    def corrupt(files):
        names = [n for n in files if n.startswith("blobs/sha256/")]
        files[names[0]] = files[names[0]] + b"tampered"

    error = repack_expect_error(_mutate_layout(corrupt))
    assert "size mismatch" in error or "digest mismatch" in error


def test_size_mismatch_rejected():
    def shrink(files):
        names = [n for n in files if n.startswith("blobs/sha256/")]
        files[names[0]] = files[names[0]][:-1]

    error = repack_expect_error(_mutate_layout(shrink))
    assert "size mismatch" in error


def test_corrupted_index_json_rejected():
    error = repack_expect_error(_mutate_layout(lambda f: f.update({"index.json": b"{"})))
    assert "invalid JSON" in error


def test_missing_oci_layout_rejected():
    error = repack_expect_error(_mutate_layout(lambda f: f.pop("oci-layout")))
    assert "missing oci-layout" in error


def test_missing_index_rejected():
    error = repack_expect_error(_mutate_layout(lambda f: f.pop("index.json")))
    assert "missing index.json" in error


def test_traversal_in_archive_rejected():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    error = repack_expect_error(buf.getvalue())
    assert "unsafe" in error


def test_symlink_in_archive_rejected():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        link = tarfile.TarInfo("index.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    error = repack_expect_error(buf.getvalue())
    assert "unsupported member type" in error


def test_not_a_tar_rejected():
    error = repack_expect_error(b"not a tar at all" * 100)
    assert "not a valid uncompressed tar" in error


def test_truncated_archive_rejected():
    # Strip the mandatory end-of-archive blocks after the last member.
    raw = build_shared_layout()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        last_end = 0
        for member in tf.getmembers():
            end = member.offset_data + (
                (member.size + 511) // 512
            ) * 512
            last_end = max(last_end, end)
    error = repack_expect_error(raw[:last_end])
    assert "truncated" in error


def test_truncated_inside_member_rejected():
    raw = build_shared_layout()
    error = repack_expect_error(raw[: len(raw) // 2])
    assert "truncated" in error


def test_unsupported_layer_media_type_rejected():
    builder = LayoutBuilder()
    builder.add_image(
        architecture="amd64",
        layers=[("application/vnd.oci.image.layer.v1.tar+zstd", SHARED)],
    )
    error = repack_expect_error(builder.build())
    assert "unsupported layer media type" in error


def test_excessively_nested_index_rejected():
    # Chain 20 nested indexes; anything deeper than the limit is rejected.
    builder = LayoutBuilder()
    builder.add_image(architecture="amd64", layers=[(OCI_LAYER_TAR, SHARED)])
    files = _read_tar(builder.build(nested_index=True))
    leaf = json.loads(files["index.json"])["manifests"][0]
    current = leaf
    for _ in range(20):
        nested = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [current],
        }
        raw = json.dumps(nested, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        files["blobs/sha256/" + digest] = raw
        current = {"mediaType": OCI_INDEX, "digest": "sha256:" + digest, "size": len(raw)}
    files["index.json"] = json.dumps(
        {"schemaVersion": 2, "manifests": [current]}
    ).encode()
    error = repack_expect_error(_tar(files))
    assert "too deep" in error


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_http_repack_returns_downloadable_tar():
    resp = client.post(
        "/repack",
        files={"layout": ("layout.tar", io.BytesIO(build_shared_layout()), "application/x-tar")},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-tar"
    assert "repacked-layout.tar" in resp.headers["content-disposition"]

    files = _read_tar(resp.content)
    index = list_index(files)
    assert len(index) == 2
    assert {d["platform"]["architecture"] for d in index} == {"amd64", "arm64"}
    _, snap = import_image(
        files, platform={"architecture": "amd64", "os": "linux"}, tag="repo:latest"
    )
    assert "shared/base" in snap


def test_http_repack_invalid_layout_returns_400_no_download():
    bad = _mutate_layout(lambda f: f.pop(next(n for n in f if n.startswith("blobs/"))))
    resp = client.post(
        "/repack",
        files={"layout": ("layout.tar", io.BytesIO(bad), "application/x-tar")},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/json"
    assert "detail" in resp.json()
    assert "content-disposition" not in resp.headers


def test_http_repack_missing_field_returns_422():
    resp = client.post("/repack", files={"wrong": ("x", io.BytesIO(b"x"), "text/plain")})
    assert resp.status_code == 422


def test_http_repack_empty_upload_returns_400():
    resp = client.post(
        "/repack", files={"layout": ("layout.tar", io.BytesIO(b""), "application/x-tar")}
    )
    assert resp.status_code == 400


def test_openapi_documents_repack():
    spec = client.get("/openapi.json").json()
    assert "/repack" in spec["paths"]
    assert "/merge" in spec["paths"]


# --------------------------------------------------------------------------- #
# stricter validation / robustness
# --------------------------------------------------------------------------- #
def test_compressed_layout_archive_rejected():
    error = repack_expect_error(gzip.compress(build_shared_layout()))
    assert "compressed" in error


def test_gzip_layer_without_gzip_magic_rejected():
    builder = LayoutBuilder()
    builder.add_image(architecture="amd64", layers=[(OCI_LAYER_TAR, SHARED)])
    files = _read_tar(builder.build(nested_index=False))
    fake = b"definitely-not-gzip"
    fake_digest = hashlib.sha256(fake).hexdigest()
    files["blobs/sha256/" + fake_digest] = fake
    index = json.loads(files["index.json"])
    manifest_digest = index["manifests"][0]["digest"].split(":")[1]
    manifest = json.loads(files["blobs/sha256/" + manifest_digest])
    manifest["layers"][0] = {
        "mediaType": OCI_LAYER_GZIP,
        "digest": "sha256:" + fake_digest,
        "size": len(fake),
    }
    broken = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    files["blobs/sha256/" + hashlib.sha256(broken).hexdigest()] = broken
    index["manifests"][0]["digest"] = "sha256:" + hashlib.sha256(broken).hexdigest()
    index["manifests"][0]["size"] = len(broken)
    files["index.json"] = json.dumps(index).encode()

    error = repack_expect_error(_tar(files))
    assert "does not start with a gzip header" in error


def test_uncompressed_layer_that_is_actually_gzip_rejected():
    builder = LayoutBuilder()
    builder.add_image(
        architecture="amd64", layers=[(OCI_LAYER_TAR, GZIPPED)]
    )
    error = repack_expect_error(builder.build())
    assert "is gzip-compressed" in error


def test_manifest_without_config_rejected():
    builder = LayoutBuilder()
    builder.add_image(architecture="amd64", layers=[(OCI_LAYER_TAR, SHARED)])
    files = _read_tar(builder.build(nested_index=False))
    index = json.loads(files["index.json"])
    digest = index["manifests"][0]["digest"].split(":")[1]
    manifest = json.loads(files["blobs/sha256/" + digest])
    manifest.pop("config")
    broken = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    files["blobs/sha256/" + hashlib.sha256(broken).hexdigest()] = broken
    index["manifests"][0]["digest"] = "sha256:" + hashlib.sha256(broken).hexdigest()
    index["manifests"][0]["size"] = len(broken)
    files["index.json"] = json.dumps(index).encode()
    error = repack_expect_error(_tar(files))
    assert "no config descriptor" in error


def test_unsupported_manifest_media_type_rejected():
    builder = LayoutBuilder()
    builder.add_image(
        architecture="amd64",
        layers=[(OCI_LAYER_TAR, SHARED)],
        manifest_media_type="application/vnd.example.unknown+json",
    )
    error = repack_expect_error(builder.build())
    # The builder stores the same media type both in the blob and descriptor.
    assert "unsupported manifest media type" in error or "not an image config" in error


def test_conversion_failure_aborts_whole_package(monkeypatch):
    """If the gzip round-trip check fails, no artifact may be produced."""
    import app.layout as layout_module

    monkeypatch.setattr(
        layout_module.LayoutRepacker, "_streams_equal", staticmethod(lambda *a: False)
    )
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "layout.tar")
        with open(src, "wb") as fh:
            fh.write(build_shared_layout())
        output = os.path.join(tmp, "repacked-layout.tar")
        with pytest.raises(LayoutError) as excinfo:
            repack_layout_tar(src, tmp, output)
        assert "does not decompress to the original bytes" in str(excinfo.value)
        assert not os.path.exists(output)


def test_failed_repack_leaves_no_output_artifact():
    bad = _mutate_layout(lambda f: f.pop(next(n for n in f if n.startswith("blobs/"))))
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "layout.tar")
        with open(src, "wb") as fh:
            fh.write(bad)
        output = os.path.join(tmp, "repacked-layout.tar")
        with pytest.raises(LayoutError):
            repack_layout_tar(src, tmp, output)
        assert not os.path.exists(output)


def test_http_repack_cleans_up_request_directory():
    import app.main as main_module

    created = []
    real_mkdtemp = main_module.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    main_module.tempfile.mkdtemp = tracking_mkdtemp
    try:
        ok = client.post(
            "/repack",
            files={
                "layout": (
                    "layout.tar",
                    io.BytesIO(build_shared_layout()),
                    "application/x-tar",
                )
            },
        )
        assert ok.status_code == 200
        bad = _mutate_layout(
            lambda f: f.pop(next(n for n in f if n.startswith("blobs/")))
        )
        assert (
            client.post(
                "/repack",
                files={"layout": ("layout.tar", io.BytesIO(bad), "application/x-tar")},
            ).status_code
            == 400
        )
    finally:
        main_module.tempfile.mkdtemp = real_mkdtemp

    assert created
    for path in created:
        assert not os.path.exists(path), f"request directory leaked: {path}"
