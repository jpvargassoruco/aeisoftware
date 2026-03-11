from fastapi import APIRouter, UploadFile, File, HTTPException
import boto3, os
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
    s3 = _s3()
    try:
        # Ensure bucket exists
        try:
            s3.create_bucket(Bucket=S3_BUCKET)
        except Exception:
            pass
        resp = s3.list_objects_v2(Bucket=S3_BUCKET)
        items = [
            {"key": o["Key"], "size_mb": round(o["Size"] / 1024 / 1024, 1),
             "last_modified": o["LastModified"].isoformat()}
            for o in resp.get("Contents", [])
        ]
        return {"templates": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{path:path}", status_code=201)
async def upload_template(path: str, file: UploadFile = File(...)):
    """Upload a pg_dump file to Ceph RGW. path = e.g. 'v18/starter.dump'"""
    s3 = _s3()
    try:
        try:
            s3.create_bucket(Bucket=S3_BUCKET)
        except Exception:
            pass
        s3.upload_fileobj(file.file, S3_BUCKET, path)
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
