#!/usr/bin/env python
"""End-to-end self-test for ``POST /repack`` of the running service.

Builds an OCI image layout with **two platforms that share a layer** plus an
already-gzip layer, posts the layout archive, downloads the repacked archive
and verifies (using only the standard library):

* ``index.json`` lists image manifests directly (no nested index),
* every platform can be imported through ``index.json``,
* all layers are gzip, the shared layer maps to a single blob,
* every converted layer decompresses byte-for-byte to the original blob,
* the original uncompressed layer blobs are not shipped,
* a layout with a missing blob is rejected with HTTP 400 and no download.

Run against a locally started service, e.g.::

    python -I scripts/selftest_repack.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
REF_NAME = "org.opencontainers.image.ref.name"


# --------------------------------------------------------------------------- #
# layout construction (stdlib only)
# --------------------------------------------------------------------------- #
def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _desc(media_type: str, raw: bytes) -> dict:
    return {"mediaType": media_type, "digest": "sha256:" + _sha(raw), "size": len(raw)}


def build_layer(entries: List[Tuple]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.PAX_FORMAT) as tf:
        for entry in entries:
            if entry[0] == "dir":
                info = tarfile.TarInfo(entry[1])
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                _, path, data = entry
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def build_layout() -> Tuple[bytes, dict]:
    shared = build_layer([("dir", "shared/"), ("file", "shared/base", b"shared\n")])
    amd = build_layer([("file", "arch/amd64", b"amd\n")])
    arm = build_layer([("file", "arch/arm64", b"arm\n")])
    pre_gzip = gzip.compress(
        build_layer([("file", "already/gz", b"kept\n")]), mtime=0
    )

    blobs: Dict[str, bytes] = {}
    for payload in (shared, amd, arm, pre_gzip):
        blobs[_sha(payload)] = payload

    manifests = []
    for architecture, extra in (("amd64", amd), ("arm64", arm)):
        config = json.dumps(
            {
                "architecture": architecture,
                "os": "linux",
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [
                        "sha256:" + _sha(shared),
                        "sha256:" + _sha(extra),
                        "sha256:" + _sha(gzip.decompress(pre_gzip)),
                    ],
                },
                "config": {},
            },
            sort_keys=True,
        ).encode()
        blobs[_sha(config)] = config
        layers = [
            _desc(OCI_LAYER_TAR, shared),
            _desc(OCI_LAYER_TAR, extra),
            _desc(OCI_LAYER_GZIP, pre_gzip),
        ]
        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": _desc(OCI_CONFIG, config),
            "layers": layers,
        }
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        blobs[_sha(raw)] = raw
        desc = _desc(OCI_MANIFEST, raw)
        desc["platform"] = {"architecture": architecture, "os": "linux"}
        manifests.append(desc)

    nested = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": manifests,
    }
    nested_raw = json.dumps(nested, sort_keys=True, separators=(",", ":")).encode()
    blobs[_sha(nested_raw)] = nested_raw
    top = _desc(OCI_INDEX, nested_raw)
    top["annotations"] = {REF_NAME: "demo:latest"}

    index = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [top]}
    files = {
        "oci-layout": json.dumps({"imageLayoutVersion": "1.0.0"}).encode(),
        "index.json": json.dumps(index).encode(),
        **{f"blobs/sha256/{d}": raw for d, raw in blobs.items()},
    }
    return _pack(files), {
        "shared": shared,
        "amd": amd,
        "arm": arm,
        "pre_gzip": pre_gzip,
        "blobs": blobs,
    }


def _pack(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack(raw: bytes) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for member in tf.getmembers():
            if member.isreg():
                src = tf.extractfile(member)
                files[member.name] = src.read() if src is not None else b""
    return files


# --------------------------------------------------------------------------- #
# independent importer: index.json -> manifest -> gzip layers -> file tree
# --------------------------------------------------------------------------- #
def import_platform(
    files: Dict[str, bytes], architecture: str, tag: str
) -> Dict[str, bytes]:
    def blob(desc: dict) -> bytes:
        digest = desc["digest"].split(":", 1)[1]
        data = files["blobs/sha256/" + digest]
        assert hashlib.sha256(data).hexdigest() == digest
        assert len(data) == desc["size"]
        return data

    def find(descriptors: List[dict], inherited_tag: Optional[str]) -> Optional[dict]:
        for desc in descriptors:
            annotations = desc.get("annotations") or {}
            current_tag = annotations.get(REF_NAME) or inherited_tag
            if desc["mediaType"] in (
                OCI_INDEX,
                "application/vnd.docker.distribution.manifest.list.v2+json",
            ):
                inner = json.loads(blob(desc))
                hit = find(inner["manifests"], current_tag)
                if hit is not None:
                    return hit
                continue
            if (desc.get("platform") or {}).get("architecture") != architecture:
                continue
            if tag and current_tag != tag:
                continue
            return desc
        return None

    descriptor = find(json.loads(files["index.json"])["manifests"], None)
    assert descriptor is not None, f"platform {architecture} not found"

    manifest = json.loads(blob(descriptor))
    tree: Dict[str, bytes] = {}
    for layer in manifest["layers"]:
        data = blob(layer)
        assert layer["mediaType"] in (OCI_LAYER_GZIP, OCI_LAYER_TAR)
        if layer["mediaType"].endswith("+gzip"):
            data = gzip.decompress(data)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            for member in tf.getmembers():
                if member.isdir():
                    continue
                src = tf.extractfile(member)
                tree[member.name] = src.read() if src is not None else b""
    return tree


# --------------------------------------------------------------------------- #
# HTTP (stdlib only)
# --------------------------------------------------------------------------- #
def post_layout(base_url: str, data: bytes) -> Tuple[int, bytes, dict]:
    boundary = "----selftest-" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        b'Content-Disposition: form-data; name="layout"; filename="layout.tar"\r\n'
    )
    body.write(b"Content-Type: application/x-tar\r\n\r\n")
    body.write(data)
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        base_url.rstrip("/") + "/repack",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read(), {
                k.lower(): v for k, v in resp.headers.items()
            }
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    failures = 0

    def check(condition: bool, message: str) -> None:
        nonlocal failures
        if condition:
            print(f"[ OK ] {message}")
        else:
            failures += 1
            print(f"[FAIL] {message}")

    layout_tar, source = build_layout()
    status, body, headers = post_layout(args.base_url, layout_tar)
    if status != 200:
        print(f"[FAIL] repack: HTTP {status}: {body[:300]!r}")
        return 1
    check(headers.get("content-type") == "application/x-tar", "response is a tar")
    check(
        "repacked-layout.tar" in headers.get("content-disposition", ""),
        "attachment filename is repacked-layout.tar",
    )

    files = _unpack(body)
    index = json.loads(files["index.json"])
    descriptors = index["manifests"]
    check(len(descriptors) == 2, "top-level index lists two image manifests")
    check(
        all(d["mediaType"] == OCI_MANIFEST for d in descriptors),
        "top-level index entries are image manifests, not nested indexes",
    )
    check(
        {d["platform"]["architecture"] for d in descriptors} == {"amd64", "arm64"},
        "both platforms preserved",
    )
    check(
        all(
            (d.get("annotations") or {}).get(REF_NAME) == "demo:latest"
            for d in descriptors
        ),
        "tag annotation preserved",
    )

    shared_digests = set()
    gzip_blobs = []
    layout_digests = {
        name.rsplit("/", 1)[-1] for name in files if name.startswith("blobs/sha256/")
    }
    for desc in descriptors:
        manifest = json.loads(files["blobs/sha256/" + desc["digest"].split(":")[1]])
        for layer in manifest["layers"]:
            raw = files["blobs/sha256/" + layer["digest"].split(":")[1]]
            check(
                hashlib.sha256(raw).hexdigest() == layer["digest"].split(":")[1]
                and len(raw) == layer["size"],
                f"{desc['platform']['architecture']}: {layer['mediaType']} digest/size updated",
            )
            check(layer["mediaType"] == OCI_LAYER_GZIP, "layer media type is +gzip")
            gzip_blobs.append(raw)
        shared_digests.add(manifest["layers"][0]["digest"])

    check(len(shared_digests) == 1, "shared layer compressed to exactly one blob")

    check(
        _sha(source["shared"]) not in layout_digests
        and _sha(source["amd"]) not in layout_digests
        and _sha(source["arm"]) not in layout_digests,
        "uncompressed source layers are not shipped",
    )
    check(
        _sha(source["pre_gzip"]) in layout_digests
        and files["blobs/sha256/" + _sha(source["pre_gzip"])] == source["pre_gzip"],
        "already-gzip layer kept byte-for-byte",
    )

    for raw in gzip_blobs:
        decompressed = gzip.decompress(raw)
        check(
            decompressed in (source["shared"], source["amd"], source["arm"],
                             gzip.decompress(source["pre_gzip"])),
            "gzip layer decompresses to an original payload",
        )

    amd_tree = import_platform(files, "amd64", "demo:latest")
    arm_tree = import_platform(files, "arm64", "demo:latest")
    check(amd_tree.get("shared/base") == b"shared\n", "amd64 imports shared base layer")
    check(arm_tree.get("shared/base") == b"shared\n", "arm64 imports shared base layer")
    check(amd_tree.get("arch/amd64") == b"amd\n", "amd64 imports its platform layer")
    check(arm_tree.get("arch/arm64") == b"arm\n", "arm64 imports its platform layer")
    check(amd_tree.get("already/gz") == b"kept\n", "pre-gzip layer imports correctly")

    # Failure path: drop a referenced blob; the whole package must be rejected.
    broken = _unpack(layout_tar)
    victim = next(name for name in broken if name.startswith("blobs/sha256/"))
    del broken[victim]
    status, body, headers = post_layout(args.base_url, _pack(broken))
    check(status == 400, f"missing blob rejected with 400 (got {status})")
    check("json" in headers.get("content-type", ""), "error is JSON")
    check("content-disposition" not in headers, "no download offered on failure")
    if "json" in headers.get("content-type", ""):
        print(f"       detail: {json.loads(body)['detail'][:120]}")

    print()
    if failures:
        print(f"RESULT: {failures} failure(s)")
        return 1
    print("RESULT: all repack self-test checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
