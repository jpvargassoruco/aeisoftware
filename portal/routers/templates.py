"""
S3 template management — optimized.

Key optimizations:
  - S3 client singleton (shared with instances.py via _s3() helper)
  - Template list caching (30s TTL to reduce S3 API calls)
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
import boto3, os, zipfile, io, time
from botocore.client import Config
from k8s_utils.manifests import S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET

router = APIRouter()

# ─── S3 Singleton ──────────────────────────────────────────────────────────────

_s3_client = None


def _s3():
    """Return cached S3 client (singleton pattern)."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            verify=False,
        )
    return _s3_client


# ─── Template List Cache (30s TTL) ────────────────────────────────────────────

_template_cache: dict | None = None
_template_cache_time: float = 0
_TEMPLATE_CACHE_TTL = 30  # seconds


@router.get("")
async def list_templates():
    """List available Odoo ZIP backup templates (only .zip files).

    OPTIMIZATION: 30s TTL cache avoids hitting S3 on every poll.
    The portal UI refreshes templates frequently; without cache each
    call takes ~50-100ms to S3. With cache: <1ms for hot path.
    """
    global _template_cache, _template_cache_time
    now = time.monotonic()
    if _template_cache is not None and (now - _template_cache_time) < _TEMPLATE_CACHE_TTL:
        return _template_cache

    s3 = _s3()
    try:
        try:
            s3.create_bucket(Bucket=S3_BUCKET)
        except Exception:
            pass
        resp = s3.list_objects_v2(Bucket=S3_BUCKET)
        items = [
            {"key": o["Key"], "size_mb": round(o["Size"] / 1024 / 1024, 1),
             "last_modified": o["LastModified"].isoformat()}
            for o in resp.get("Contents", [])
            if o["Key"].endswith(".zip")
        ]
        result = {"templates": items}
        _template_cache = result
        _template_cache_time = now
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{path:path}", status_code=201)
async def upload_template(path: str, file: UploadFile = File(...)):
    """Upload an Odoo ZIP backup to Ceph RGW."""
    global _template_cache
    # 1. Extension validation
    if not path.lower().endswith(".zip"):
        raise HTTPException(
            status_code=422,
            detail="Only Odoo ZIP backups are accepted (file must end with .zip)."
        )

    # 2. Read content and validate ZIP structure
    content = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            if "dump.sql" not in names:
                raise HTTPException(status_code=422, detail="Invalid: ZIP does not contain 'dump.sql'.")
            if "manifest.json" not in names:
                raise HTTPException(status_code=422, detail="Invalid: ZIP does not contain 'manifest.json'.")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="Not a valid ZIP archive.")

    # 3. Upload to S3
    s3 = _s3()
    try:
        try:
            s3.create_bucket(Bucket=S3_BUCKET)
        except Exception:
            pass
        s3.upload_fileobj(io.BytesIO(content), S3_BUCKET, path)
        _template_cache = None  # Invalidate cache on upload
        return {"uploaded": path, "bucket": S3_BUCKET, "endpoint": S3_ENDPOINT}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{path:path}", status_code=204)
async def delete_template(path: str):
    global _template_cache
    s3 = _s3()
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=path)
        _template_cache = None  # Invalidate cache on delete
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return None
