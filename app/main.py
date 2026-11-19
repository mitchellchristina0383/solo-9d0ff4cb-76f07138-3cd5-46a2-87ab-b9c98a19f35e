"""FastAPI service: OCI layer tooling.

* ``POST /merge`` merges two consecutive uncompressed OCI diff layers.
* ``POST /repack`` turns an uploaded OCI Image Layout tar into a transport
  friendly archive: every uncompressed layer is streamed through gzip (shared
  layers only once), already-gzip layers are kept, the per-platform image
  manifests are rewritten and the top level index is flattened to reference
  them directly.

Uploads are streamed to private spool files first; an artifact is only
produced (and therefore downloadable) when all inputs have been fully
validated.  Any invalid input yields HTTP 400 and no artifact.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .layout import LayoutError, cleanup_workdir, repack_layout_tar
from .merger import InvalidLayer, LayerMerger

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per layer upload


def _env_bytes(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MAX_LAYOUT_ARCHIVE_BYTES = _env_bytes(
    "REPACK_MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024
)  # 4 GiB layout archive by default
READ_CHUNK = 1024 * 1024

app = FastAPI(
    title="OCI Layer Tools",
    version="2.0.0",
    description=(
        "Layer tooling for air-gapped transport. `POST /merge` combines two "
        "consecutive uncompressed OCI diff layers (ordered lower then upper) "
        "into a single reusable diff layer. `POST /repack` rewrites an OCI "
        "Image Layout tar so that every uncompressed layer becomes a gzip "
        "layer (each shared blob compressed only once), manifests and the top "
        "level index are updated, and the resulting archive can be imported "
        "per platform."
    ),
)


class HealthResponse(BaseModel):
    status: str


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _spool_upload(
    upload: UploadFile, workdir: str, max_bytes: int = MAX_UPLOAD_BYTES
) -> str:
    """Stream an upload into a private spool file, enforcing the size cap."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="missing upload filename")
    safe_name = os.path.basename(upload.filename).replace("/", "_") or "layer.tar"
    upload_dir = os.path.join(workdir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    spool = os.path.join(upload_dir, safe_name)
    total = 0
    try:
        with open(spool, "wb") as dst:
            while True:
                chunk = upload.file.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {max_bytes} byte limit",
                    )
                dst.write(chunk)
    finally:
        upload.file.close()
    if total == 0:
        raise HTTPException(status_code=400, detail=f"{safe_name} is empty")
    return spool


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/merge",
    tags=["layers"],
    responses={
        200: {
            "content": {"application/x-tar": {}},
            "description": "Merged uncompressed layer tar",
        },
        400: {"description": "Invalid input layer"},
        413: {"description": "Upload too large"},
    },
)
def merge_layers(
    lower: UploadFile = File(..., description="lower layer tar, applied first"),
    upper: UploadFile = File(..., description="upper layer tar, applied second"),
) -> FileResponse:
    """Merge two ordered diff layers and return the combined layer tar."""
    workdir = tempfile.mkdtemp(prefix="layermerger-req-")
    try:
        lower_path = _spool_upload(lower, workdir)
        upper_path = _spool_upload(upper, workdir)
        output_path = os.path.join(workdir, "merged-layer.tar")

        try:
            with LayerMerger() as merger:
                merger.add_layer(lower_path, origin=1)
                merger.add_layer(upper_path, origin=2)
                with open(output_path, "wb") as out:
                    merger.write_tar(out)
        except InvalidLayer as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, EOFError) as exc:
            raise HTTPException(status_code=400, detail=f"unreadable tar: {exc}") from exc

        return FileResponse(
            output_path,
            media_type="application/x-tar",
            filename="merged-layer.tar",
            background=BackgroundTask(_cleanup, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception:
        _cleanup(workdir)
        raise


@app.post(
    "/repack",
    tags=["layout"],
    responses={
        200: {
            "content": {"application/x-tar": {}},
            "description": "Repacked OCI Image Layout archive",
        },
        400: {"description": "Invalid layout, missing blob or conversion failure"},
        413: {"description": "Upload too large"},
    },
)
def repack_layout(
    layout: UploadFile = File(..., description="OCI Image Layout tar (uncompressed)"),
) -> FileResponse:
    """Compress uncompressed layers and flatten the layout for transport.

    The upload must be an uncompressed tar containing an OCI image layout.
    Every layer is rewritten in place; the resulting ``index.json`` lists the
    per-platform image manifests directly.  The download is only offered once
    the whole layout has been validated.
    """
    workdir = tempfile.mkdtemp(prefix="layoutrepack-req-")
    try:
        upload_path = _spool_upload(layout, workdir, MAX_LAYOUT_ARCHIVE_BYTES)
        output_path = os.path.join(workdir, "repacked-layout.tar")
        try:
            repack_layout_tar(upload_path, workdir, output_path)
        except LayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return FileResponse(
            output_path,
            media_type="application/x-tar",
            filename="repacked-layout.tar",
            background=BackgroundTask(cleanup_workdir, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception:
        _cleanup(workdir)
        raise
