from fastapi import (
    FastAPI, Form, UploadFile, Request, Depends, HTTPException, Cookie
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, FileResponse
)
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

# ----------------------------------------------------------
# Boiler-plate identical to your original file
# ----------------------------------------------------------
load_dotenv()
app = FastAPI(title="Course Management System")

@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health_check():
    return {"status": "healthy"}

@app.get("/debug-config")
async def debug_config():
    return {
        "s3_bucket": os.getenv("S3_BUCKET"),
        "s3_endpoint": "https://objstorage.leapcell.io",
        "redis_host": os.getenv("REDIS_URI"),
        "temp_dir": "/tmp",
        "temp_dir_exists": os.path.exists("/tmp"),
        "temp_dir_writable": os.access("/tmp", os.W_OK),
    }

@app.get("/test-s3")
async def test_s3():
    try:
        response = s3.list_buckets()
        return {"status": "S3 connection successful",
                "buckets": [b['Name'] for b in response['Buckets']]}
    except Exception as e:
        logger.error(f"S3 test failed: {e}")
        return {"status": "S3 connection failed", "error": str(e)}

s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",
    aws_access_key_id=os.getenv("ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("SECRET_ACCESS_KEY")
)
S3_BUCKET = os.getenv("S3_BUCKET")
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
# Utility functions (unchanged)
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
    try:
        s3.download_file(S3_BUCKET, s3_key, local_path)
        logger.info(f"Downloaded {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error downloading from S3: {e}")
        raise HTTPException(status_code=500,
                            detail="Failed to download file from storage")

def delete_from_s3(s3_key: str):
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"Deleted {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error deleting from S3: {e}")
        raise HTTPException(status_code=500,
                            detail="Failed to delete file from storage")

# ----------------------------------------------------------
# Web routes (unchanged)
# ----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    courses = get_courses()
    return templates.TemplateResponse("user_dashboard.html",
                                      {"request": request, "courses": courses})

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login_post(
        response: RedirectResponse,
        email: str = Form(...),
        password: str = Form(...)
):
    if email == ADMIN_EMAIL and pwd_context.verify(password, ADMIN_PASSWORD_HASH):
        session_id = create_session()
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="session_id", value=session_id,
                            httponly=True, secure=True)
        return response
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request,
                          session_id: Optional[str] = Cookie(None)):
    if not is_logged_in(session_id):
        return RedirectResponse(url="/admin/login", status_code=303)
    courses = get_courses()
    return templates.TemplateResponse("admin_dashboard.html",
                                      {"request": request, "courses": courses})

@app.get("/logout")
async def admin_logout(response: RedirectResponse,
                       session_id: Optional[str] = Cookie(None)):
    if session_id:
        redis_client.delete(f"session:{session_id}")
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key="session_id")
    return response

@app.post("/add-course")
async def add_course(title: str = Form(...)):
    courses = get_courses()
    courses.append({"title": title, "plans": []})
    save_courses(courses)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/edit-course")
async def edit_course(course_index: int = Form(...), title: str = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        courses[course_index]["title"] = title
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...),
                   name: str = Form(...),
                   file: UploadFile = None):
    logger.info(f"Starting add-plan for course_index: {course_index}, name: {name}")
    if file:
        logger.info(f"File provided: {file.filename}, content_type: {file.content_type}")
        if file.content_type != "application/pdf":
            raise HTTPException(status_code=400,
                                detail="Invalid file type. Only PDF files allowed.")
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413,
                                detail="File size exceeds 10MB limit")
        await file.seek(0)

    courses = get_courses()
    if 0 <= course_index < len(courses):
        try:
            if file and file.filename:
                filename = sanitize_filename(file.filename)
                temp_pdf_path = os.path.join(temp_dir, filename)
                with open(temp_pdf_path, "wb") as f:
                    f.write(await file.read())
                upload_to_s3(temp_pdf_path, filename)
                os.remove(temp_pdf_path)
                courses[course_index]["plans"].append(
                    {"name": name, "filename": filename})
            else:
                courses[course_index]["plans"].append(
                    {"name": name, "filename": None})
            save_courses(courses)
            return RedirectResponse(url="/admin", status_code=303)
        except Exception as e:
            logger.error(f"Error in add_plan: {e}")
            raise HTTPException(status_code=500,
                                detail=f"Failed to add plan: {e}")
    raise HTTPException(status_code=404, detail="Course not found")

@app.post("/edit-plan")
async def edit_plan(course_index: int = Form(...),
                    plan_index: int = Form(...),
                    name: str = Form(...),
                    file: UploadFile = None):
    courses = get_courses()
    if 0 <= course_index < len(courses) and \
       0 <= plan_index < len(courses[course_index]["plans"]):
        try:
            courses[course_index]["plans"][plan_index]["name"] = name
            if file and file.filename:
                if file.content_type != "application/pdf":
                    raise HTTPException(status_code=400,
                                        detail="Invalid file type. Only PDF files allowed.")
                contents = await file.read()
                if len(contents) > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413,
                                        detail="File size exceeds 10MB limit")
                await file.seek(0)

                old_filename = courses[course_index]["plans"][plan_index]["filename"]
                if old_filename:
                    delete_from_s3(old_filename)

                filename = sanitize_filename(file.filename)
                temp_pdf_path = os.path.join(temp_dir, filename)
                with open(temp_pdf_path, "wb") as f:
                    f.write(await file.read())
                upload_to_s3(temp_pdf_path, filename)
                os.remove(temp_pdf_path)
                courses[course_index]["plans"][plan_index]["filename"] = filename

            save_courses(courses)
            return RedirectResponse(url="/admin", status_code=303)
        except Exception as e:
            logger.error(f"Error editing plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to edit plan")
    raise HTTPException(status_code=404, detail="Plan not found")

# ----------------------------------------------------------
# FIXED DOWNLOAD HANDLER
# ----------------------------------------------------------
@app.get("/download/{filename}")
async def download_pdf(filename: str):
    """
    Stream a PDF straight from S3 through a local temporary file.
    The temporary file is NOT deleted here; it will be removed when
    the container restarts.  If you need immediate cleanup, spawn
    an asyncio task (example in the comment).
    """
    temp_pdf_path = os.path.join(temp_dir, filename)

    try:
        download_from_s3(filename, temp_pdf_path)
        if not os.path.isfile(temp_pdf_path):
            raise HTTPException(status_code=404, detail="File not found")

        # ------------------------------------------------------------------
        # If you really want to clean up immediately, uncomment the block
        # below and import asyncio at the top.
        # ------------------------------------------------------------------
        # import asyncio
        # async def cleanup():
        #     await asyncio.sleep(5)          # give the OS time to send bytes
        #     try:
        #         os.remove(temp_pdf_path)
        #     except Exception:
        #         pass
        # asyncio.create_task(cleanup())

        return FileResponse(
            temp_pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        # Propagate 404 from download_from_s3
        raise
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        raise HTTPException(status_code=500, detail="Failed to download file")

# ----------------------------------------------------------
# Other unchanged routes
# ----------------------------------------------------------
@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        for plan in courses[course_index]["plans"]:
            if plan["filename"]:
                try:
                    delete_from_s3(plan["filename"])
                except Exception as e:
                    logger.error(f"Error deleting file {plan['filename']} from S3: {e}")
        courses.pop(course_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")

@app.post("/delete-plan")
async def delete_plan(course_index: int = Form(...),
                      plan_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses) and \
       0 <= plan_index < len(courses[course_index]["plans"]):
        filename = courses[course_index]["plans"][plan_index]["filename"]
        if filename:
            try:
                delete_from_s3(filename)
            except Exception as e:
                logger.error(f"Error deleting file {filename} from S3: {e}")
        courses[course_index]["plans"].pop(plan_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")

# ----------------------------------------------------------
# Exception handlers (unchanged)
# ----------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request,
                                        exc: StarletteHTTPException):
    if exc.status_code == HTTP_404_NOT_FOUND:
        return templates.TemplateResponse(
            "404.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "error_message": exc.detail or "Page not found",
            },
            status_code=exc.status_code,
        )
    elif exc.status_code == 413:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "error_message": "File too large. Maximum size is 10MB.",
            },
            status_code=exc.status_code,
        )
    return HTMLResponse(content=str(exc.detail), status_code=exc.status_code)

@app.get("/logs", response_class=FileResponse)
async def download_logs():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            f.write("Log file created.\n")
    temp_log_path = os.path.join(temp_dir, "logs_download.txt")
    with open(LOG_FILE, "r") as source, open(temp_log_path, "w") as target:
        target.write(source.read())
    return FileResponse(temp_log_path, media_type="text/plain", filename="logs.txt")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    logger.error(f"Traceback: {traceback.format_exc()}")
    return HTMLResponse(content="An unexpected error occurred", status_code=500)

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
