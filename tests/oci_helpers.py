"""Helpers to build and import OCI Image Layout archives in tests.

The builders deliberately write layouts the way real tooling does: image
manifests reference blobs by digest, a multi-platform layout keeps a nested
index blob, and tags live in ``org.opencontainers.image.ref.name``
annotations.  The importer is a small independent consumer that resolves a
platform through ``index.json`` only.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from typing import Dict, List, Optional, Tuple

from tests.helpers import ReferenceFS

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
REF_NAME = "org.opencontainers.image.ref.name"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def descriptor(media_type: str, raw: bytes) -> dict:
    return {
        "mediaType": media_type,
        "digest": "sha256:" + sha256(raw),
        "size": len(raw),
    }


class LayoutBuilder:
    """Accumulate blobs and construct a layout archive."""

    def __init__(self) -> None:
        self.blobs: Dict[str, bytes] = {}
        self.images: List[dict] = []
        self.index_annotations: Optional[dict] = None

    # -- blobs ------------------------------------------------------------ #
    def add_blob(self, raw: bytes) -> dict:
        digest = sha256(raw)
        self.blobs[digest] = raw
        return {"digest": "sha256:" + digest, "size": len(raw)}

    # -- images ----------------------------------------------------------- #
    def add_image(
        self,
        *,
        architecture: str = "amd64",
        os_name: str = "linux",
        variant: Optional[str] = None,
        tag: Optional[str] = None,
        layers: Optional[List[Tuple[str, bytes]]] = None,
        manifest_media_type: str = OCI_MANIFEST,
        config_media_type: str = OCI_CONFIG,
        config_overrides: Optional[dict] = None,
        platform_override: Optional[dict] = None,
    ) -> dict:
        """Add one image; returns its manifest descriptor (with platform)."""
        diff_ids = []
        for media_type, data in layers or []:
            plain = gzip.decompress(data) if media_type.endswith("+gzip") else data
            diff_ids.append("sha256:" + sha256(plain))

        config = {
            "architecture": architecture,
            "os": os_name,
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
            "config": {},
        }
        if variant is not None:
            config["variant"] = variant
        if config_overrides:
            config.update(config_overrides)
        config_raw = json.dumps(config, sort_keys=True).encode()
        config_desc = descriptor(config_media_type, config_raw)
        self.blobs[sha256(config_raw)] = config_raw

        layer_descs = []
        for media_type, data in layers or []:
            desc = descriptor(media_type, data)
            desc["mediaType"] = media_type
            self.blobs[sha256(data)] = data
            layer_descs.append(desc)
        manifest = {
            "schemaVersion": 2,
            "mediaType": manifest_media_type,
            "config": config_desc,
            "layers": layer_descs,
        }
        manifest_raw = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        manifest_desc = descriptor(manifest_media_type, manifest_raw)
        self.blobs[sha256(manifest_raw)] = manifest_raw

        platform = platform_override or {"architecture": architecture, "os": os_name}
        if variant is not None and platform_override is None:
            platform["variant"] = variant
        image = {"platform": platform, "tag": tag, "manifest": manifest_desc}
        self.images.append(image)
        return image

    # -- archive ---------------------------------------------------------- #
    def build(
        self,
        *,
        nested_index: bool = True,
        duplicate_at_root: bool = False,
        top_annotations: Optional[dict] = None,
    ) -> bytes:
        if nested_index:
            inner_manifests = []
            for image in self.images:
                desc = dict(image["manifest"])
                desc["platform"] = dict(image["platform"])
                inner_manifests.append(desc)
            inner = {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": inner_manifests,
            }
            inner_raw = json.dumps(
                inner, sort_keys=True, separators=(",", ":")
            ).encode()
            inner_desc = descriptor(OCI_INDEX, inner_raw)
            self.blobs[sha256(inner_raw)] = inner_raw

            tags = sorted({i["tag"] for i in self.images if i["tag"]})
            top_manifests = []
            if tags:
                for tag in tags:
                    desc = dict(inner_desc)
                    desc["annotations"] = {REF_NAME: tag}
                    top_manifests.append(desc)
            else:
                top_manifests.append(inner_desc)
        else:
            top_manifests = []
            for image in self.images:
                desc = dict(image["manifest"])
                desc["platform"] = dict(image["platform"])
                if image["tag"]:
                    desc["annotations"] = {REF_NAME: image["tag"]}
                top_manifests.append(desc)

        index = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": top_manifests,
        }
        if top_annotations:
            index["annotations"] = top_annotations

        layout = {"imageLayoutVersion": "1.0.0"}
        return _tar(
            {
                "oci-layout": json.dumps(layout).encode(),
                "index.json": json.dumps(index).encode(),
                **{
                    f"blobs/sha256/{digest}": raw
                    for digest, raw in self.blobs.items()
                },
            }
        )


def _tar(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def layer_tar(entries) -> bytes:
    """Reuse the reference builder from tests.helpers for layer contents."""
    from tests.helpers import build_tar

    return build_tar(entries)


# --------------------------------------------------------------------------- #
# independent layout importer (uses only index.json)
# --------------------------------------------------------------------------- #
def _read_tar(tar_bytes: bytes) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            assert member.isreg(), f"unexpected member type: {member.name}"
            src = tf.extractfile(member)
            files[member.name] = src.read() if src is not None else b""
    return files


def list_index(files: Dict[str, bytes]) -> List[dict]:
    return json.loads(files["index.json"])["manifests"]


def import_image(
    files: Dict[str, bytes],
    *,
    platform: Optional[dict] = None,
    tag: Optional[str] = None,
) -> Tuple[dict, Dict[str, Tuple[str, bytes]]]:
    """Resolve an image through ``index.json`` and apply its layers.

    Nested indexes are followed like a real importer would (platform and tag
    annotations are inherited by their children).  Returns
    ``(manifest, reference-snapshot)``.  Every descriptor digest and size is
    verified on the way, so a corrupted repack fails loudly.
    """
    chosen = _find_manifest(
        files, list_index(files), platform, tag, inherited_tag=None
    )
    if chosen is None:
        raise LookupError(f"no manifest for platform={platform!r} tag={tag!r}")

    raw = _blob(files, chosen)
    manifest = json.loads(raw)
    assert manifest["schemaVersion"] == 2
    _blob(files, manifest["config"])

    fs = ReferenceFS()
    for layer_desc in manifest["layers"]:
        data = _blob(files, layer_desc)
        media_type = layer_desc["mediaType"]
        if media_type.endswith("+gzip") or media_type.endswith(".gzip"):
            data = gzip.decompress(data)
        elif media_type.endswith("+zstd") or "zstd" in media_type:
            raise AssertionError("unexpected zstd layer")
        fs.apply(data)
    return manifest, fs.snapshot()


def _find_manifest(
    files: Dict[str, bytes],
    descriptors: List[dict],
    platform: Optional[dict],
    tag: Optional[str],
    inherited_tag: Optional[str],
) -> Optional[dict]:
    for desc in descriptors:
        annotations = desc.get("annotations") or {}
        desc_tag = annotations.get(REF_NAME) or inherited_tag
        if desc["mediaType"] in (OCI_INDEX, "application/vnd.docker.distribution.manifest.list.v2+json"):
            inner = json.loads(_blob(files, desc))
            found = _find_manifest(
                files, inner["manifests"], platform, tag, desc_tag
            )
            if found is not None:
                return found
            continue
        if platform is not None:
            desc_platform = desc.get("platform") or {}
            if any(desc_platform.get(k) != v for k, v in platform.items()):
                continue
        if tag is not None and desc_tag != tag:
            continue
        return desc
    return None


def _blob(files: Dict[str, bytes], desc: dict) -> bytes:
    digest = desc["digest"]
    algorithm, _, hex_part = digest.partition(":")
    assert algorithm == "sha256"
    data = files.get("blobs/sha256/" + hex_part)
    assert data is not None, f"missing blob {digest}"
    assert len(data) == desc["size"], f"size mismatch for {digest}"
    assert hashlib.sha256(data).hexdigest() == hex_part, f"digest mismatch {digest}"
    return data


def import_layout_tar(
    tar_bytes: bytes, **kwargs
) -> Tuple[dict, Dict[str, Tuple[str, bytes]]]:
    return import_image(_read_tar(tar_bytes), **kwargs)
