from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
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

load_dotenv()

# Increase file upload size limit (100MB)
app = FastAPI(
    max_upload_size=100 * 1024 * 1024,  # 100MB
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]
)

# Configure S3 client
s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "dc60b71e92ad4c5b8ce6916b6a822544"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "61a724c0aceda49a211ffdd2db53c5ce1fdd5b3bb02f31aecc53f496434dd6ac")
)

S3_BUCKET = os.getenv("S3_BUCKET", "schedule-bgx6-5e6c-77raidoi")

# Use /tmp directory for temporary files
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
        redis_client.set("courses", json.dumps(courses, default=lambda o: o.dict()))
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
        s3.upload_file(file_path, S3_BUCKET, s3_key)
        logger.info(f"Uploaded {s3_key} to S3")
    except Exception as e:
        logger.error(f"Error uploading to S3: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload file to storage.")

def download_from_s3(s3_key: str, local_path: str):
    try:
        s3.download_file(S3_BUCKET, s3_key, local_path)
        logger.info(f"Downloaded {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error downloading from S3: {e}")
        raise HTTPException(status_code=500, detail="Failed to download file from storage.")

def delete_from_s3(s3_key: str):
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"Deleted {s3_key} from S3")
    except Exception as e:
        logger.error(f"Error deleting from S3: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete file from storage.")

# Health check endpoint
@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health_check():
    return JSONResponse(content={"status": "healthy", "message": "Server is running"})

@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    courses = get_courses()
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login_post(response: RedirectResponse, email: str = Form(...), password: str = Form(...)):
    if email == ADMIN_EMAIL and pwd_context.verify(password, ADMIN_PASSWORD_HASH):
        session_id = create_session()
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="session_id", value=session_id, httponly=True, secure=True)
        return response
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, session_id: Optional[str] = Cookie(None)):
    if not is_logged_in(session_id):
        return RedirectResponse(url="/admin/login", status_code=303)
    courses = get_courses()
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})

@app.get("/logout")
async def admin_logout(response: RedirectResponse, session_id: Optional[str] = Cookie(None)):
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
async def add_plan(course_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files are allowed.")
    
    # Increased file size limit to 50MB
    if file.size > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds limit (50MB)")

    courses = get_courses()
    if 0 <= course_index < len(courses):
        try:
            filename = sanitize_filename(file.filename)
            # Save file temporarily
            temp_pdf_path = os.path.join(temp_dir, filename)
            with open(temp_pdf_path, "wb") as f:
                content = await file.read()
                f.write(content)
            
            # Upload to S3
            upload_to_s3(temp_pdf_path, filename)
            
            # Clean up temp file
            os.remove(temp_pdf_path)
            
            courses[course_index]["plans"].append({"name": name, "filename": filename})
            save_courses(courses)
        except Exception as e:
            logger.error(f"Error adding plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to add plan")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")

@app.post("/edit-plan")
async def edit_plan(course_index: int = Form(...), plan_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    courses = get_courses()
    if 0 <= course_index < len(courses) and 0 <= plan_index < len(courses[course_index]["plans"]):
        try:
            # Update plan name
            courses[course_index]["plans"][plan_index]["name"] = name
            
            # If a new file is provided, update it
            if file and file.filename:
                if file.content_type != "application/pdf":
                    raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files are allowed.")
                
                # Increased file size limit to 50MB
                if file.size > 50 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="File size exceeds limit (50MB)")
                
                # Delete old file from S3
                old_filename = courses[course_index]["plans"][plan_index]["filename"]
                delete_from_s3(old_filename)
                
                # Upload new file
                filename = sanitize_filename(file.filename)
                temp_pdf_path = os.path.join(temp_dir, filename)
                with open(temp_pdf_path, "wb") as f:
                    content = await file.read()
                    f.write(content)
                
                upload_to_s3(temp_pdf_path, filename)
                os.remove(temp_pdf_path)
                
                courses[course_index]["plans"][plan_index]["filename"] = filename
            
            save_courses(courses)
        except Exception as e:
            logger.error(f"Error editing plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to edit plan")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")

@app.get("/download/{filename}")
async def download_pdf(filename: str):
    # Create temp file path
    temp_pdf_path = os.path.join(temp_dir, filename)
    
    try:
        # Download from S3 to temp location
        download_from_s3(filename, temp_pdf_path)
        
        if not os.path.isfile(temp_pdf_path):
            raise HTTPException(status_code=404, detail="File not found")
        
        # Return file response
        response = FileResponse(
            temp_pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
        # Clean up temp file after response is sent
        @response.on_close
        def cleanup_temp_file():
            try:
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
            except Exception as e:
                logger.error(f"Error cleaning up temp file: {e}")
        
        return response
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        raise HTTPException(status_code=500, detail="Failed to download file")

@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        # Delete all associated files from S3
        for plan in courses[course_index]["plans"]:
            try:
                delete_from_s3(plan["filename"])
            except Exception as e:
                logger.error(f"Error deleting file {plan['filename']} from S3: {e}")
        
        courses.pop(course_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")

@app.post("/delete-plan")
async def delete_plan(course_index: int = Form(...), plan_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses) and 0 <= plan_index < len(courses[course_index]["plans"]):
        # Delete file from S3
        filename = courses[course_index]["plans"][plan_index]["filename"]
        try:
            delete_from_s3(filename)
        except Exception as e:
            logger.error(f"Error deleting file {filename} from S3: {e}")
        
        courses[course_index]["plans"].pop(plan_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
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
    return HTMLResponse(content=str(exc.detail), status_code=exc.status_code)

@app.get("/logs", response_class=FileResponse)
async def download_logs():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w") as f:
            f.write("Log file created.\n")
    
    # Create a temporary copy for download
    temp_log_path = os.path.join(temp_dir, "logs_download.txt")
    with open(LOG_FILE, "r") as source, open(temp_log_path, "w") as target:
        target.write(source.read())
    
    response = FileResponse(temp_log_path, media_type="text/plain", filename="logs.txt")
    
    # Clean up temp file after response is sent
    @response.on_close
    def cleanup_temp_file():
        try:
            if os.path.exists(temp_log_path):
                os.remove(temp_log_path)
        except Exception as e:
            logger.error(f"Error cleaning up temp file: {e}")
    
    return response
            
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return HTMLResponse(content="An unexpected error occurred", status_code=500)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENVIRONMENT", "development") == "development",
        workers=int(os.getenv("WORKERS", 1)),
        log_level="info",
        # Add these to handle larger file uploads
        limit_concurrency=100,
        limit_max_requests=1000,
    )
