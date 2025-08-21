# main.py
from fastapi import (
    FastAPI, Form, UploadFile, Request, HTTPException, Cookie
)
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
import logging
import uvicorn
import traceback
from pathlib import Path

load_dotenv()

# ------------------------------------------------------------------
# FastAPI & static set-up
# ------------------------------------------------------------------
app = FastAPI(title="Course Management System")

@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health_check():
    return {"status": "healthy"}

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
LOG_FILE = "/tmp/logs.txt"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Redis
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# S3
# ------------------------------------------------------------------
s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",
    aws_access_key_id=os.getenv("ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("SECRET_ACCESS_KEY"),
    config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
)
S3_BUCKET = os.getenv("S3_BUCKET")

# ------------------------------------------------------------------
# Password hashing
# ------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = pwd_context.hash(os.getenv("ADMIN_PASSWORD"))

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def sanitize_filename(filename: str) -> str:
    """
    Return a filesystem/URL-safe name **without** any extra uuid.
    We keep the original extension and replace unsafe chars with '_'.
    """
    path = Path(filename)
    stem = re.sub(r'[^a-zA-Z0-9_-]', '_', path.stem)
    extension = path.suffix.lower()
    return f"{stem}{extension}"

def get_courses():
    try:
        data = redis_client.get("courses")
        return json.loads(data) if data else []
    except Exception as e:
        logger.error(f"get_courses: {e}")
        return []

def save_courses(courses):
    try:
        redis_client.set("courses", json.dumps(courses))
    except Exception as e:
        logger.error(f"save_courses: {e}")
        raise HTTPException(500, "Failed to save courses.")

def create_session():
    sid = str(uuid.uuid4())
    redis_client.setex(f"session:{sid}", 3600, "logged_in")
    return sid

def is_logged_in(session_id: Optional[str]) -> bool:
    try:
        return bool(session_id and redis_client.get(f"session:{session_id}") == "logged_in")
    except Exception:
        return False

# ------------------------------------------------------------------
# S3 helpers – same key everywhere
# ------------------------------------------------------------------
def upload_to_s3(file_path: str, s3_key: str):
    try:
        logger.info(f"Uploading {file_path} -> s3://{S3_BUCKET}/{s3_key}")
        s3.upload_file(file_path, S3_BUCKET, s3_key,
                       ExtraArgs={"ContentType": "application/pdf"})
    except Exception as e:
        logger.error(f"upload_to_s3: {e}")
        raise HTTPException(500, f"S3 upload failed: {e}")

def download_from_s3(s3_key: str, local_path: str):
    try:
        logger.info(f"Downloading s3://{S3_BUCKET}/{s3_key} -> {local_path}")
        s3.download_file(S3_BUCKET, s3_key, local_path)
    except Exception as e:
        logger.error(f"download_from_s3: {e}")
        raise HTTPException(500, f"S3 download failed: {e}")

def delete_from_s3(s3_key: str):
    try:
        logger.info(f"Deleting s3://{S3_BUCKET}/{s3_key}")
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        logger.error(f"delete_from_s3: {e}")
        raise HTTPException(500, f"S3 delete failed: {e}")

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    return templates.TemplateResponse("user_dashboard.html",
                                      {"request": request, "courses": get_courses()})

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login_post(
        email: str = Form(...),
        password: str = Form(...)
):
    if email == ADMIN_EMAIL and pwd_context.verify(password, ADMIN_PASSWORD_HASH):
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie(key="session_id", value=create_session(),
                        httponly=True, secure=True)
        return resp
    raise HTTPException(401, "Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request,
                          session_id: Optional[str] = Cookie(None)):
    if not is_logged_in(session_id):
        return RedirectResponse(url="/admin/login", status_code=303)
    return templates.TemplateResponse("admin_dashboard.html",
                                      {"request": request, "courses": get_courses()})

@app.get("/logout")
async def logout(session_id: Optional[str] = Cookie(None)):
    if session_id:
        redis_client.delete(f"session:{session_id}")
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie("session_id")
    return resp

# ------------------------------------------------------------------
# Course/Plan CRUD – all use exactly the same key
# ------------------------------------------------------------------
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
    raise HTTPException(404, "Course not found")

@app.post("/add-plan")
async def add_plan(
        course_index: int = Form(...),
        name: str = Form(...),
        file: Optional[UploadFile] = None
):
    courses = get_courses()
    if not (0 <= course_index < len(courses)):
        raise HTTPException(404, "Course not found")

    filename = None
    if file and file.filename:
        if file.content_type != "application/pdf":
            raise HTTPException(400, "Only PDF allowed")
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(413, "File too large")

        filename = sanitize_filename(file.filename)
        tmp_path = f"/tmp/{filename}"
        with open(tmp_path, "wb") as f:
            f.write(contents)
        upload_to_s3(tmp_path, filename)
        os.remove(tmp_path)

    courses[course_index]["plans"].append({"name": name, "filename": filename})
    save_courses(courses)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/edit-plan")
async def edit_plan(
        course_index: int = Form(...),
        plan_index: int = Form(...),
        name: str = Form(...),
        file: Optional[UploadFile] = None
):
    courses = get_courses()
    if not (0 <= course_index < len(courses) and
            0 <= plan_index < len(courses[course_index]["plans"])):
        raise HTTPException(404, "Plan not found")

    plan = courses[course_index]["plans"][plan_index]
    old_key = plan.get("filename")

    plan["name"] = name

    if file and file.filename:
        if file.content_type != "application/pdf":
            raise HTTPException(400, "Only PDF allowed")
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(413, "File too large")

        new_key = sanitize_filename(file.filename)
        tmp_path = f"/tmp/{new_key}"
        with open(tmp_path, "wb") as f:
            f.write(contents)
        upload_to_s3(tmp_path, new_key)
        os.remove(tmp_path)

        if old_key:
            delete_from_s3(old_key)
        plan["filename"] = new_key

    save_courses(courses)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        for plan in courses[course_index]["plans"]:
            if plan.get("filename"):
                delete_from_s3(plan["filename"])
        courses.pop(course_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(404, "Course not found")

@app.post("/delete-plan")
async def delete_plan(course_index: int = Form(...),
                      plan_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses) and \
       0 <= plan_index < len(courses[course_index]["plans"]):
        plan = courses[course_index]["plans"][plan_index]
        key = plan.get("filename")
        if key:
            delete_from_s3(key)
        courses[course_index]["plans"].pop(plan_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(404, "Plan not found")

# ------------------------------------------------------------------
# Download – uses the exact same key
# ------------------------------------------------------------------
@app.get("/download/{filename}")
async def download_pdf(filename: str):
    tmp_path = f"/tmp/{filename}"
    download_from_s3(filename, tmp_path)
    return FileResponse(
        tmp_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ------------------------------------------------------------------
# Error handlers & logs (unchanged)
# ------------------------------------------------------------------
@app.get("/logs")
async def download_logs():
    return FileResponse(LOG_FILE, media_type="text/plain", filename="logs.txt")

# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
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
