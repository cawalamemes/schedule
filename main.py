from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from typing import Optional
import os
import json
import redis
import re
import uuid
import boto3
from botocore.config import Config
import tempfile
from dotenv import load_dotenv
from pathlib import Path
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_404_NOT_FOUND
import logging
import uvicorn
import traceback
import asyncio          # only needed if you enable the bg cleanup

load_dotenv()
app = FastAPI(title="Course Management System")

# ----------------------------------------------------------
# Health & debug endpoints
# ----------------------------------------------------------
@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health_check():
    return {"status": "healthy"}

@app.get("/debug-config")
async def debug_config():
    return {
        "s3_bucket": S3_BUCKET,
        "s3_endpoint": "https://objstorage.leapcell.io",
        "redis_host": os.getenv("REDIS_URI"),
        "temp_dir": "/tmp",
        "temp_dir_exists": os.path.exists("/tmp"),
        "temp_dir_writable": os.access("/tmp", os.W_OK),
    }

# NEW -------------------------------------------------------
@app.get("/debug-s3-list")
async def list_bucket():
    """
    Return every key currently stored in the configured bucket.
    """
    try:
        paginator = s3.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=S3_BUCKET):
            for obj in page.get('Contents', []):
                keys.append(obj['Key'])
        return {"bucket": S3_BUCKET, "objects": keys}
    except Exception as e:
        logger.error(f"debug-s3-list error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug-s3-head/{filename}")
async def head_object(filename: str):
    """
    Run HeadObject to see if the key exists and what S3 replies.
    """
    try:
        resp = s3.head_object(Bucket=S3_BUCKET, Key=filename)
        return {"bucket": S3_BUCKET, "key": filename, "headers": dict(resp)}
    except Exception as e:
        logger.error(f"debug-s3-head error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# ----------------------------------------------------------

# ----------------------------------------------------------
# S3 client
# ----------------------------------------------------------
s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",
    aws_access_key_id=os.getenv("ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("SECRET_ACCESS_KEY")
)
S3_BUCKET = os.getenv("S3_BUCKET")

# ----------------------------------------------------------
# Everything below is identical to the previous fixed file
# ----------------------------------------------------------
temp_dir = "/tmp"
pdfs_dir = os.path.join(temp_dir, "pdfs")
os.makedirs(pdfs_dir, exist_ok=True)

LOG_FILE = os.path.join(temp_dir, "logs.txt")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

REDIS = os.getenv("REDIS_URI")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
host, port = REDIS.split(":")
redis_client = redis.StrictRedis(
    host=host,
    port=int(port),
    password=REDIS_PASSWORD,
    decode_responses=True,
    ssl=True,
)

try:
    redis_client.ping()
    logger.info("Connected to Redis!")
except redis.AuthenticationError:
    logger.error("Authentication failed")
except redis.ConnectionError:
    logger.error("Connection error")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = pwd_context.hash(os.getenv("ADMIN_PASSWORD"))

# ----------------------------------------------------------
# Utility helpers (unchanged)
# ----------------------------------------------------------
def sanitize_filename(filename: str) -> str:
    path = Path(filename)
    stem = path.stem
    extension = path.suffix.lower()
    stem = re.sub(r'[^a-zA-Z0-9_-]', '', stem.replace(' ', '_'))
    stem = re.sub(r'_+', '_', stem).strip('_')
    if not stem:
        stem = "file"
    unique_id = uuid.uuid4().hex[:6]
    return f"{stem}_{unique_id}{extension}"

def get_courses():
    try:
        courses_json = redis_client.get("courses")
        return json.loads(courses_json) if courses_json else []
    except Exception as e:
        logger.error(f"Error fetching courses: {e}")
        return []

def save_courses(courses):
    try:
        redis_client.set("courses", json.dumps(courses))
    except Exception as e:
        logger.error(f"Error saving courses: {e}")
        raise HTTPException(status_code=500, detail="Failed to save courses.")

def create_session():
    session_id = str(uuid.uuid4())
    try:
        redis_client.setex(f"session:{session_id}", 3600, "logged_in")
    except Exception as e:
        logger.error(f"Error creating session: {e}")
    return session_id

def is_logged_in(session_id: Optional[str]) -> bool:
    try:
        if not session_id:
            return False
        return redis_client.get(f"session:{session_id}") == "logged_in"
    except Exception as e:
        logger.error(f"Error checking session: {e}")
        return False

def upload_to_s3(file_path: str, s3_key: str):
    try:
        logger.info(f"Starting S3 upload: {file_path} -> {S3_BUCKET}/{s3_key}")
        if not os.path.exists(file_path):
            raise Exception(f"File does not exist: {file_path}")
        file_size = os.path.getsize(file_path)
        logger.info(f"File size for upload: {file_size} bytes")
        if file_size == 0:
            raise Exception("File is empty")
        s3.upload_file(
            file_path,
            S3_BUCKET,
            s3_key,
            ExtraArgs={'ContentType': 'application/pdf'}
        )
        logger.info(f"Successfully uploaded {s3_key} to S3 bucket {S3_BUCKET}")
    except Exception as e:
        logger.error(f"Error uploading to S3: {e}")
        raise HTTPException(status_code=500,
                            detail=f"Failed to upload file to storage: {e}")

def download_from_s3(s3_key: str, local_path: str):
    """
    Unchanged – but the exception is now propagated so we see the real reason.
    """
    try:
        s3.download_file(S3_BUCKET, s3_key, local_path)
        logger.info(f"Downloaded {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error downloading from S3: {e}")
        raise HTTPException(status_code=500,
                            detail=f"Failed to download file from storage: {e}")

def delete_from_s3(s3_key: str):
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"Deleted {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error deleting from S3: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete file from storage.")

# ----------------------------------------------------------
# Web routes (unchanged – omitted for brevity)
# ----------------------------------------------------------
# … (same as previous file) …

# ----------------------------------------------------------
# Download endpoint (unchanged – temp file NOT deleted)
# ----------------------------------------------------------
@app.get("/download/{filename}")
async def download_pdf(filename: str):
    temp_pdf_path = os.path.join(temp_dir, filename)
    try:
        download_from_s3(filename, temp_pdf_path)
        if not os.path.isfile(temp_pdf_path):
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            temp_pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        raise HTTPException(status_code=500, detail="Failed to download file")

# ----------------------------------------------------------
# Exception handlers (unchanged)
# ----------------------------------------------------------
# … (same as previous file) …

# ----------------------------------------------------------
# Entry-point (unchanged)
# ----------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENVIRONMENT", "development") == "development",
        workers=int(os.getenv("WORKERS", 1)),
        log_level="info",
        limit_concurrency=100,
        limit_max_requests=1000,
    )
