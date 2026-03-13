from fastapi import APIRouter, UploadFile, File, HTTPException
import boto3, os, zipfile, io
from botocore.client import Config
from k8s_utils.manifests import S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET

router = APIRouter()


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        verify=False,
    )


@router.get("")
async def list_templates():
    """List available Odoo ZIP backup templates (only .zip files)."""
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
            if o["Key"].endswith(".zip")   # only ZIP templates
        ]
        return {"templates": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{path:path}", status_code=201)
async def upload_template(path: str, file: UploadFile = File(...)):
    """Upload an Odoo ZIP backup to Ceph RGW.
    
    Only .zip files are accepted. The ZIP must contain:
    - dump.sql   (PostgreSQL plain-text dump)
    - manifest.json (Odoo backup manifest)
    
    path = e.g. 'v18/my-backup.zip'
    """
    # 1. Extension validation
    if not path.lower().endswith(".zip"):
        raise HTTPException(
            status_code=422,
            detail="Only Odoo ZIP backups are accepted (file must end with .zip). "
                   "Create one via Settings → Technical → Database → Backup → ZIP format."
        )

    # 2. Read content and validate ZIP structure
    content = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            if "dump.sql" not in names:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid Odoo backup: ZIP does not contain 'dump.sql'. "
                           "Make sure to use the ZIP backup format from Odoo's database manager."
                )
            if "manifest.json" not in names:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid Odoo backup: ZIP does not contain 'manifest.json'. "
                           "Make sure to use the ZIP backup format from Odoo's database manager."
                )
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=422,
            detail="The uploaded file is not a valid ZIP archive."
        )

    # 3. Upload to S3
    s3 = _s3()
    try:
        try:
            s3.create_bucket(Bucket=S3_BUCKET)
        except Exception:
            pass
        s3.upload_fileobj(io.BytesIO(content), S3_BUCKET, path)
        return {"uploaded": path, "bucket": S3_BUCKET, "endpoint": S3_ENDPOINT}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{path:path}", status_code=204)
async def delete_template(path: str):
    s3 = _s3()
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return None
