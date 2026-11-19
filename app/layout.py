"""Repack an OCI image layout archive for transport into an offline network.

The input is a tar archive containing an `OCI Image Layout`_ at its root (or
below a single enclosing directory).  Every *uncompressed* image layer that is
reachable from ``index.json`` is streamed through gzip; layers that are already
gzip-compressed are copied unchanged.  The resulting layout is flattened so
that the top-level index points directly at the per-platform image manifests,
and every manifest descriptor in that index is updated to the rewritten
manifest.

Design goals:

* **byte-exact layers** – after compressing a layer the gzip stream is
  decompressed again and compared byte-for-byte with the source blob before it
  is accepted;
* **compress shared blobs once** – several manifests may reference the same
  uncompressed layer; it is compressed exactly once and the resulting digest is
  reused;
* **atomic failure** – missing blobs, digest/size mismatches, unsupported
  content and compression failures raise :class:`LayoutError` before an output
  archive is produced, so the HTTP layer can refuse to offer a download;
* **no dead weight** – only blobs reachable from the rewritten index are
  written, so superseded uncompressed layers and old/nested indexes disappear.

.. _OCI Image Layout: https://github.com/opencontainers/image-spec/blob/main/image-layout.md
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# media types
# --------------------------------------------------------------------------- #
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
INDEX_TYPES = {OCI_INDEX, DOCKER_MANIFEST_LIST}

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_TYPES = {OCI_MANIFEST, DOCKER_MANIFEST}

OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
CONFIG_TYPES = {OCI_CONFIG, DOCKER_CONFIG}

# Uncompressed layer -> the gzip media type that replaces it.
UNCOMPRESSED_TO_GZIP = {
    "application/vnd.oci.image.layer.v1.tar":
        "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar":
        "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar":
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.docker.image.rootfs.foreign.diff.tar":
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
}

# Layers that are already gzip-compressed are copied verbatim.
GZIP_LAYER_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
}

REF_NAME = "org.opencontainers.image.ref.name"

_GZIP_MAGIC = b"\x1f\x8b"

# gzip compression level used for converted layers.  Level 9 favours the
# smallest possible transport artifact; override with REPACK_GZIP_LEVEL.
DEFAULT_GZIP_LEVEL = 9


def _gzip_level() -> int:
    raw = os.environ.get("REPACK_GZIP_LEVEL", "")
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_GZIP_LEVEL
    return level if 1 <= level <= 9 else DEFAULT_GZIP_LEVEL

# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
CHUNK = 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_INDEX_DEPTH = 16
MAX_MANIFEST_REFS = 100_000
MAX_TAR_MEMBERS = 1_000_000
MAX_CONFIG_INFER_BYTES = 2 * 1024 * 1024


class LayoutError(ValueError):
    """Raised when an uploaded OCI layout cannot be repacked."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _sha256_file(path: str) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _parse_digest(value: object, context: str) -> str:
    """Validate a ``sha256:<hex>`` digest and return the lowercase hex part."""
    if not isinstance(value, str):
        raise LayoutError(f"{context}: descriptor digest is not a string")
    algorithm, sep, hex_part = value.partition(":")
    if sep != ":" or algorithm != "sha256":
        raise LayoutError(
            f"{context}: unsupported digest {value!r} (only sha256 is supported)"
        )
    if len(hex_part) != 64:
        raise LayoutError(f"{context}: malformed sha256 digest {value!r}")
    try:
        int(hex_part, 16)
    except ValueError as exc:
        raise LayoutError(f"{context}: malformed sha256 digest {value!r}") from exc
    return hex_part.lower()


def _descriptor_size(desc: dict, context: str) -> int:
    size = desc.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise LayoutError(f"{context}: descriptor has no valid non-negative size")
    return size


def _annotations(desc: dict, context: str) -> Dict[str, str]:
    raw = desc.get("annotations")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()
    ):
        raise LayoutError(f"{context}: annotations must be a string-to-string map")
    return dict(raw)


def _platform(desc: dict, context: str) -> Optional[dict]:
    raw = desc.get("platform")
    if raw is None:
        return None
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise LayoutError(f"{context}: platform must be an object with string keys")
    return dict(raw)


def _load_json_bytes(raw: bytes, context: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayoutError(f"{context}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LayoutError(f"{context}: expected a JSON object")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# input archive extraction
# --------------------------------------------------------------------------- #
def _normalise_member_name(name: str, is_dir: bool, context: str) -> Optional[str]:
    if "\x00" in name or "\\" in name:
        raise LayoutError(f"{context}: illegal characters in member name {name!r}")
    if name.startswith("/"):
        raise LayoutError(f"{context}: absolute member name {name!r} is not allowed")
    if not is_dir and name.endswith("/"):
        raise LayoutError(f"{context}: regular file {name!r} has a directory name")
    if is_dir:
        name = name.rstrip("/")
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise LayoutError(f"{context}: unsafe member name {name!r}")
    return name


def _assert_proper_tar_end(tf: tarfile.TarFile, total_size: int, context: str) -> None:
    """Reject truncated archives (tarfile otherwise tolerates a missing end)."""
    fobj = tf.fileobj
    if fobj is None:  # pragma: no cover - always a real file here
        return
    end_blocks = tarfile.BLOCKSIZE * 2
    if total_size < end_blocks:
        raise LayoutError(f"{context}: truncated tar (missing end-of-archive blocks)")

    expected_end = 0
    saw_member = False
    fobj.seek(0)
    while True:
        header_pos = fobj.tell()
        header = fobj.read(tarfile.BLOCKSIZE)
        if len(header) < tarfile.BLOCKSIZE:
            raise LayoutError(f"{context}: truncated tar (incomplete header block)")
        if header == b"\x00" * tarfile.BLOCKSIZE:
            break
        try:
            info = tarfile.TarInfo.frombuf(
                header, encoding="utf-8", errors="surrogateescape"
            )
        except (tarfile.HeaderError, tarfile.InvalidHeaderError) as exc:
            raise LayoutError(f"{context}: malformed tar header: {exc}") from exc
        saw_member = True
        data_blocks = (info.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
        expected_end = header_pos + tarfile.BLOCKSIZE + data_blocks * tarfile.BLOCKSIZE
        if expected_end > total_size:
            raise LayoutError(
                f"{context}: truncated tar (member payload past end of file)"
            )
        fobj.seek(expected_end)

    if not saw_member:
        fobj.seek(0)
        if fobj.read(end_blocks) != b"\x00" * end_blocks:
            raise LayoutError(f"{context}: truncated tar (missing end-of-archive blocks)")
        return
    if expected_end + end_blocks > total_size:
        raise LayoutError(f"{context}: truncated tar (missing end-of-archive blocks)")
    fobj.seek(expected_end)
    if fobj.read(end_blocks) != b"\x00" * end_blocks:
        raise LayoutError(f"{context}: truncated tar (missing end-of-archive blocks)")


def extract_layout_archive(tar_path: str, destination: str) -> str:
    """Safely extract an uncompressed layout tar and return the layout root.

    Only regular files and directories are accepted.  Absolute paths, ``..``
    components, links, devices and FIFOs are rejected before anything is
    written outside ``destination``.
    """
    os.makedirs(destination, exist_ok=True)
    total_size = os.path.getsize(tar_path)
    with open(tar_path, "rb") as probe:
        if probe.read(6).startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")):
            raise LayoutError(
                "layout archive is compressed; upload an uncompressed tar"
            )
    try:
        tf = tarfile.open(name=tar_path, mode="r:")
    except tarfile.ReadError as exc:
        raise LayoutError(f"input is not a valid uncompressed tar: {exc}") from exc

    seen_files: set[str] = set()
    try:
        member_count = 0
        extracted = 0
        while True:
            try:
                member = tf.next()
            except tarfile.ReadError as exc:
                raise LayoutError(f"truncated or malformed tar: {exc}") from exc
            if member is None:
                break
            member_count += 1
            if member_count > MAX_TAR_MEMBERS:
                raise LayoutError("layout archive contains too many members")

            name = _normalise_member_name(member.name, member.isdir(), "layout archive")
            if name is None:
                if member.isdir():
                    continue
                raise LayoutError(f"layout archive: unexpected root member {member.name!r}")

            target = os.path.join(destination, *name.split("/"))
            if member.isdir():
                if name in seen_files:
                    raise LayoutError(
                        f"layout archive: {name!r} is both a file and a directory"
                    )
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as exc:
                    raise LayoutError(
                        f"layout archive: cannot create directory {name!r}: {exc}"
                    ) from exc
                continue

            if not member.isreg():
                raise LayoutError(
                    f"layout archive: unsupported member type for {member.name!r} "
                    "(only regular files and directories are allowed)"
                )
            if member.issparse():
                # Sparse payloads can inflate far beyond the archive size and
                # never appear in OCI image layouts.
                raise LayoutError(
                    f"layout archive: sparse member {member.name!r} is not supported"
                )
            if name in seen_files:
                raise LayoutError(f"layout archive: duplicate member {name!r}")
            seen_files.add(name)

            parent = os.path.dirname(target)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.isdir(target):
                    raise LayoutError(
                        f"layout archive: {name!r} is both a directory and a file"
                    )
                extracted += member.size
                if extracted > total_size + CHUNK:
                    raise LayoutError("layout archive expands beyond its own size")
                src = tf.extractfile(member)
                if src is None:
                    raise LayoutError(f"layout archive: cannot read {name!r}")
                with open(target, "wb") as dst:
                    remaining = member.size
                    while remaining:
                        try:
                            chunk = src.read(min(CHUNK, remaining))
                        except tarfile.ReadError as exc:
                            raise LayoutError(
                                f"layout archive: truncated content for {name!r}: {exc}"
                            ) from exc
                        if not chunk:
                            raise LayoutError(
                                f"layout archive: truncated content for {name!r}"
                            )
                        dst.write(chunk)
                        remaining -= len(chunk)
            except OSError as exc:
                raise LayoutError(
                    f"layout archive: cannot extract {name!r}: {exc}"
                ) from exc

        _assert_proper_tar_end(tf, total_size, f"layout archive {tar_path!r}")
    finally:
        tf.close()

    layout_dirs: List[str] = []
    index_only: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(destination):
        if "oci-layout" in filenames:
            layout_dirs.append(dirpath)
        elif "index.json" in filenames:
            index_only.append(dirpath)
    if not layout_dirs:
        if index_only:
            raise LayoutError(
                "layout archive is missing oci-layout (required by the OCI "
                "image layout specification)"
            )
        raise LayoutError("layout archive does not contain an OCI image layout")
    if len(layout_dirs) > 1:
        raise LayoutError("layout archive contains more than one OCI image layout")
    root = layout_dirs[0]
    if not os.path.isfile(os.path.join(root, "index.json")):
        raise LayoutError(
            "layout archive is missing index.json (the layout entry point)"
        )
    return root


# --------------------------------------------------------------------------- #
# repacker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceBlob:
    digest: str  # lowercase hex
    size: int
    path: str


@dataclass
class RepackResult:
    """Summary returned by :func:`repack_layout_tar`."""

    output_path: str
    image_manifests: int
    compressed_layers: int
    reused_shared_layers: int
    kept_gzip_layers: int
    output_blobs: int


class LayoutRepacker:
    def __init__(self, layout_root: str, workdir: str) -> None:
        self.root = layout_root
        self.workdir = workdir
        self._verified: Dict[str, SourceBlob] = {}
        self._gzip_cache: Dict[str, SourceBlob] = {}
        self._manifest_cache: Dict[str, Tuple[str, int, str, dict]] = {}
        self._output_blobs: Dict[str, Tuple[str, int]] = {}
        self._output_descriptors: List[dict] = []
        self._output_keys: set[str] = set()
        self._manifest_refs = 0
        self._gzip_level = _gzip_level()
        self.compressed_layers = 0
        self.reused_shared_layers = 0
        self.kept_gzip_layers = 0

    # ------------------------------------------------------------------ #
    # blob access
    # ------------------------------------------------------------------ #
    def _source_blob(self, desc: dict, context: str) -> SourceBlob:
        if "data" in desc:
            raise LayoutError(
                f"{context}: inline descriptor 'data' is not supported; the "
                "blob must be present under blobs/sha256/"
            )
        digest = _parse_digest(desc.get("digest"), context)
        size = _descriptor_size(desc, context)
        cached = self._verified.get(digest)
        if cached is not None:
            if cached.size != size:
                raise LayoutError(
                    f"{context}: descriptor size {size} does not match actual "
                    f"blob size {cached.size} for sha256:{digest}"
                )
            return cached

        path = os.path.join(self.root, "blobs", "sha256", digest)
        if not os.path.isfile(path):
            raise LayoutError(
                f"{context}: referenced blob sha256:{digest} is missing "
                f"(expected at blobs/sha256/{digest})"
            )
        actual_size = os.path.getsize(path)
        if actual_size != size:
            raise LayoutError(
                f"{context}: size mismatch for sha256:{digest}: "
                f"descriptor says {size}, blob is {actual_size} bytes"
            )
        actual_digest, actual_size = _sha256_file(path)
        if actual_digest != digest:
            raise LayoutError(
                f"{context}: digest mismatch for sha256:{digest}: "
                f"blob content hashes to sha256:{actual_digest}"
            )
        blob = SourceBlob(digest=digest, size=actual_size, path=path)
        self._verified[digest] = blob
        return blob

    def _json_blob(self, desc: dict, context: str) -> dict:
        blob = self._source_blob(desc, context)
        if blob.size > MAX_JSON_BYTES:
            raise LayoutError(f"{context}: JSON blob is too large ({blob.size} bytes)")
        with open(blob.path, "rb") as fh:
            return _load_json_bytes(fh.read(), context)

    def _register_output(self, digest: str, size: int, path: str) -> None:
        existing = self._output_blobs.get(digest)
        if existing is not None:
            if existing[1] != size:
                raise LayoutError(
                    f"internal error: blob sha256:{digest} registered with "
                    f"conflicting sizes"
                )
            return
        self._output_blobs[digest] = (path, size)

    def _store_bytes(self, raw: bytes, suffix: str) -> Tuple[str, int, str]:
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(
            self.workdir, "generated", f"{digest}.{suffix}"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)
        self._register_output(digest, len(raw), path)
        return digest, len(raw), path

    # ------------------------------------------------------------------ #
    # layer conversion
    # ------------------------------------------------------------------ #
    @staticmethod
    def _streams_equal(gzip_path: str, raw_path: str) -> bool:
        """True iff decompressing ``gzip_path`` yields exactly ``raw_path``."""

        def read_exact(stream, size: int) -> bytes:
            buf = bytearray()
            while len(buf) < size:
                chunk = stream.read(size - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
            return bytes(buf)

        try:
            with open(raw_path, "rb") as raw, gzip.open(gzip_path, "rb") as gz:
                while True:
                    expected = read_exact(raw, CHUNK)
                    actual = read_exact(gz, len(expected)) if expected else gz.read(1)
                    if actual != expected:
                        return False
                    if not expected:
                        return True
        except (OSError, EOFError, zlib.error):
            return False

    def _gzip_blob(self, source: SourceBlob) -> SourceBlob:
        """Stream ``source`` through gzip once and verify the result."""
        cached = self._gzip_cache.get(source.digest)
        if cached is not None:
            self.reused_shared_layers += 1
            return cached

        out_path = os.path.join(
            self.workdir, "recompressed", f"{source.digest}.tar.gz"
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        compressor = zlib.compressobj(
            level=self._gzip_level, method=zlib.DEFLATED, wbits=31
        )
        compressed_digest = hashlib.sha256()
        compressed_size = 0
        try:
            with open(source.path, "rb") as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    encoded = compressor.compress(chunk)
                    if encoded:
                        dst.write(encoded)
                        compressed_digest.update(encoded)
                        compressed_size += len(encoded)
                encoded = compressor.flush()
                if encoded:
                    dst.write(encoded)
                    compressed_digest.update(encoded)
                    compressed_size += len(encoded)
        except (OSError, zlib.error) as exc:
            raise LayoutError(
                f"failed to gzip layer sha256:{source.digest}: {exc}"
            ) from exc

        # The transport format is only acceptable if it round-trips exactly.
        if not self._streams_equal(out_path, source.path):
            raise LayoutError(
                f"conversion failed: gzip of layer sha256:{source.digest} does "
                "not decompress to the original bytes"
            )
        actual_digest, actual_size = _sha256_file(out_path)
        if actual_size != compressed_size or actual_digest != compressed_digest.hexdigest():
            raise LayoutError(
                f"conversion failed: gzip blob for sha256:{source.digest} changed "
                "while it was being written"
            )

        blob = SourceBlob(
            digest=actual_digest, size=actual_size, path=out_path
        )
        self._gzip_cache[source.digest] = blob
        self._register_output(blob.digest, blob.size, blob.path)
        self.compressed_layers += 1
        return blob

    def _convert_layer(self, desc: dict, context: str) -> dict:
        media_type = desc.get("mediaType")
        if not isinstance(media_type, str):
            raise LayoutError(f"{context}: layer descriptor has no mediaType")

        if media_type in UNCOMPRESSED_TO_GZIP:
            source = self._source_blob(desc, context)
            with open(source.path, "rb") as fh:
                head = fh.read(6)
            if head.startswith(_GZIP_MAGIC):
                # The descriptor claims a plain tar but the blob is gzip.
                raise LayoutError(
                    f"{context}: blob sha256:{source.digest} is declared as an "
                    "uncompressed layer but is gzip-compressed"
                )
            compressed = self._gzip_blob(source)
            result = dict(desc)
            result["mediaType"] = UNCOMPRESSED_TO_GZIP[media_type]
            result["digest"] = "sha256:" + compressed.digest
            result["size"] = compressed.size
            return result

        if media_type in GZIP_LAYER_TYPES:
            source = self._source_blob(desc, context)
            with open(source.path, "rb") as fh:
                if fh.read(2) != b"\x1f\x8b":
                    raise LayoutError(
                        f"{context}: blob sha256:{source.digest} is declared as "
                        "gzip but does not start with a gzip header"
                    )
            # Already compressed: shipped exactly as it arrived.
            self._register_output(source.digest, source.size, source.path)
            self.kept_gzip_layers += 1
            return dict(desc)

        raise LayoutError(
            f"{context}: unsupported layer media type {media_type!r} "
            "(only uncompressed tar and gzip layers can be repacked)"
        )

    # ------------------------------------------------------------------ #
    # image manifests
    # ------------------------------------------------------------------ #
    def _infer_platform(self, config: SourceBlob, context: str) -> Optional[dict]:
        if config.size > MAX_CONFIG_INFER_BYTES:
            return None
        try:
            with open(config.path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        architecture = data.get("architecture")
        os_name = data.get("os")
        if isinstance(architecture, str) and isinstance(os_name, str):
            platform = {"architecture": architecture, "os": os_name}
            variant = data.get("variant")
            if isinstance(variant, str):
                platform["variant"] = variant
            return platform
        return None

    def _rewrite_manifest(
        self, desc: dict, context: str
    ) -> Tuple[str, int, str, dict]:
        """Rewrite one image manifest; returns (digest, size, path, data)."""
        source_digest = _parse_digest(desc.get("digest"), context)
        cached = self._manifest_cache.get(source_digest)
        if cached is not None:
            return cached

        data = self._json_blob(desc, context)
        if data.get("schemaVersion") != 2:
            raise LayoutError(f"{context}: manifest schemaVersion must be 2")
        if "subject" in data:
            raise LayoutError(
                f"{context}: manifests with a 'subject' are not supported"
            )

        config = data.get("config")
        if not isinstance(config, dict):
            raise LayoutError(f"{context}: manifest has no config descriptor")
        config_media_type = config.get("mediaType")
        if config_media_type not in CONFIG_TYPES:
            raise LayoutError(
                f"{context}: manifest config media type "
                f"{config_media_type!r} is not an image config"
            )
        # Verify (and retain) the image config exactly as it was.
        config_blob = self._source_blob(config, f"{context} config")
        self._register_output(config_blob.digest, config_blob.size, config_blob.path)

        layers = data.get("layers")
        if not isinstance(layers, list):
            raise LayoutError(f"{context}: manifest has no layers array")
        rewritten_layers = []
        for position, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise LayoutError(
                    f"{context}: layer descriptor {position} must be an object"
                )
            rewritten_layers.append(
                self._convert_layer(layer, f"{context} layer {position}")
            )

        rewritten = dict(data)
        rewritten["layers"] = rewritten_layers
        raw = _canonical_json(rewritten)
        new_digest, new_size, new_path = self._store_bytes(raw, "manifest.json")
        result = (new_digest, new_size, new_path, rewritten)
        self._manifest_cache[source_digest] = result
        return result

    def _emit_manifest(
        self,
        desc: dict,
        inherited_platform: Optional[dict],
        inherited_tag: Optional[str],
        context: str,
    ) -> None:
        annotations = _annotations(desc, context)
        tag = annotations.get(REF_NAME) or inherited_tag
        if tag is not None:
            annotations[REF_NAME] = tag

        new_digest, new_size, _path, new_data = self._rewrite_manifest(
            desc, context
        )
        platform = _platform(desc, context) or inherited_platform
        if platform is None:
            config = new_data.get("config")
            if isinstance(config, dict):
                platform = self._infer_platform(
                    self._source_blob(config, f"{context} config"), context
                )

        out: dict = {
            "mediaType": desc.get("mediaType"),
            "digest": "sha256:" + new_digest,
            "size": new_size,
        }
        if platform is not None:
            out["platform"] = platform
        if annotations:
            out["annotations"] = annotations

        key = _canonical_json(out).decode("utf-8")
        if key not in self._output_keys:
            self._output_keys.add(key)
            self._output_descriptors.append(out)

    # ------------------------------------------------------------------ #
    # index traversal
    # ------------------------------------------------------------------ #
    def _expand(
        self,
        desc: dict,
        inherited_platform: Optional[dict],
        inherited_tag: Optional[str],
        stack: Tuple[str, ...],
        context: str,
    ) -> None:
        self._manifest_refs += 1
        if self._manifest_refs > MAX_MANIFEST_REFS:
            raise LayoutError("layout references too many manifests")

        if not isinstance(desc, dict):
            raise LayoutError(f"{context}: manifest descriptor must be an object")
        media_type = desc.get("mediaType")
        if media_type in INDEX_TYPES:
            digest = _parse_digest(desc.get("digest"), context)
            if digest in stack:
                raise LayoutError(f"{context}: cyclic index reference detected")
            if len(stack) >= MAX_INDEX_DEPTH:
                raise LayoutError(f"{context}: index nesting is too deep")
            data = self._json_blob(desc, context)
            if data.get("schemaVersion") != 2:
                raise LayoutError(f"{context}: index schemaVersion must be 2")
            children = data.get("manifests")
            if not isinstance(children, list):
                raise LayoutError(f"{context}: index has no manifests array")

            annotations = _annotations(desc, context)
            tag = annotations.get(REF_NAME) or inherited_tag
            platform = _platform(desc, context) or inherited_platform
            for position, child in enumerate(children):
                child_context = f"{context} child {position}"
                if not isinstance(child, dict):
                    raise LayoutError(f"{child_context}: descriptor must be an object")
                child_platform = _platform(child, child_context) or platform
                self._expand(
                    child, child_platform, tag, stack + (digest,), child_context
                )
            return

        if media_type in MANIFEST_TYPES:
            self._emit_manifest(desc, inherited_platform, inherited_tag, context)
            return

        raise LayoutError(
            f"{context}: unsupported manifest media type {media_type!r}"
        )

    # ------------------------------------------------------------------ #
    # output
    # ------------------------------------------------------------------ #
    def run(self, output_path: str) -> RepackResult:
        layout_file = os.path.join(self.root, "oci-layout")
        try:
            with open(layout_file, "rb") as fh:
                oci_layout_raw = fh.read(MAX_JSON_BYTES + 1)
        except OSError as exc:
            raise LayoutError(f"cannot read oci-layout: {exc}") from exc
        if len(oci_layout_raw) > MAX_JSON_BYTES:
            raise LayoutError("oci-layout file is too large")
        layout_data = _load_json_bytes(oci_layout_raw, "oci-layout")
        if layout_data.get("imageLayoutVersion") != "1.0.0":
            raise LayoutError(
                "unsupported image layout version "
                f"{layout_data.get('imageLayoutVersion')!r}"
            )

        index_path = os.path.join(self.root, "index.json")
        try:
            with open(index_path, "rb") as fh:
                index_raw = fh.read(MAX_JSON_BYTES + 1)
        except OSError as exc:
            raise LayoutError(f"cannot read index.json: {exc}") from exc
        if len(index_raw) > MAX_JSON_BYTES:
            raise LayoutError("index.json is too large")
        index_data = _load_json_bytes(index_raw, "index.json")
        if index_data.get("schemaVersion") != 2:
            raise LayoutError("index.json: schemaVersion must be 2")
        index_media_type = index_data.get("mediaType")
        if index_media_type is not None and index_media_type not in INDEX_TYPES:
            raise LayoutError(
                f"index.json: unsupported media type {index_media_type!r}"
            )
        manifests = index_data.get("manifests")
        if not isinstance(manifests, list):
            raise LayoutError("index.json: missing manifests array")

        for position, desc in enumerate(manifests):
            self._expand(desc, None, None, (), f"index.json manifest {position}")

        output_index: dict = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": self._output_descriptors,
        }
        annotations = _annotations(index_data, "index.json")
        if annotations:
            output_index["annotations"] = annotations
        index_out = _canonical_json(output_index)

        self._write_tar(output_path, oci_layout_raw, index_out)
        return RepackResult(
            output_path=output_path,
            image_manifests=len(self._output_descriptors),
            compressed_layers=self.compressed_layers,
            reused_shared_layers=self.reused_shared_layers,
            kept_gzip_layers=self.kept_gzip_layers,
            output_blobs=len(self._output_blobs),
        )

    def _write_tar(
        self, output_path: str, oci_layout_raw: bytes, index_raw: bytes
    ) -> None:
        def add_bytes(tf: tarfile.TarFile, name: str, raw: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            info.mode = 0o644
            info.mtime = 0
            tf.addfile(info, _BytesReader(raw))

        def add_dir(tf: tarfile.TarFile, name: str) -> None:
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            tf.addfile(info)

        try:
            partial = output_path + ".partial"
            try:
                with tarfile.open(
                    partial, mode="w:", format=tarfile.PAX_FORMAT
                ) as tf:
                    add_dir(tf, "blobs/")
                    add_dir(tf, "blobs/sha256/")
                    add_bytes(tf, "oci-layout", oci_layout_raw)
                    add_bytes(tf, "index.json", index_raw)
                    for digest in sorted(self._output_blobs):
                        path, size = self._output_blobs[digest]
                        info = tarfile.TarInfo(name=f"blobs/sha256/{digest}")
                        info.size = size
                        info.mode = 0o644
                        info.mtime = 0
                        with open(path, "rb") as fh:
                            tf.addfile(info, fh)
                os.replace(partial, output_path)
            finally:
                if os.path.exists(partial):
                    os.unlink(partial)
        except OSError as exc:
            raise LayoutError(f"cannot write output archive: {exc}") from exc


class _BytesReader:
    """Minimal read-only stream wrapper used for in-memory tar members."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._raw) - self._pos
        end = min(len(self._raw), self._pos + size)
        chunk = self._raw[self._pos:end]
        self._pos = end
        return chunk


def repack_layout_tar(
    input_tar: str, workdir: str, output_path: Optional[str] = None
) -> RepackResult:
    """Repack ``input_tar`` into a flattened, gzip-layer OCI layout tar.

    ``workdir`` must be a writable, request-private directory.  All validation
    happens before the output tar is complete; on failure a
    :class:`LayoutError` is raised and the caller must not expose the artifact.
    """
    extraction_dir = os.path.join(workdir, "extracted")
    root = extract_layout_archive(input_tar, extraction_dir)
    if output_path is None:
        output_path = os.path.join(workdir, "repacked-layout.tar")
    repacker = LayoutRepacker(root, workdir)
    return repacker.run(output_path)


def cleanup_workdir(workdir: str) -> None:
    """Best-effort removal of a request work directory."""
    shutil.rmtree(workdir, ignore_errors=True)
