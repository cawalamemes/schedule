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

load_dotenv()

# Add this to handle health checks
app = FastAPI(title="Course Management System")

# Add health check endpoints
@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health_check():
    return {"status": "healthy"}

# Debug endpoint
@app.get("/debug-config")
async def debug_config():
    return {
        "s3_bucket": os.getenv("S3_BUCKET"),
        "s3_endpoint": "https://objstorage.leapcell.io",  # Fixed: removed extra spaces
        "redis_host": os.getenv("REDIS_URI"),
        "temp_dir": "/tmp",
        "temp_dir_exists": os.path.exists("/tmp"),
        "temp_dir_writable": os.access("/tmp", os.W_OK),
    }

# Test S3 connectivity
@app.get("/test-s3")
async def test_s3():
    try:
        # List buckets to test connection
        response = s3.list_buckets()
        return {"status": "S3 connection successful", "buckets": [b['Name'] for b in response['Buckets']]}
    except Exception as e:
        logger.error(f"S3 test failed: {str(e)}")
        return {"status": "S3 connection failed", "error": str(e)}

# Configure S3 client - FIXED: removed extra spaces in endpoint_url
s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",  # Fixed: removed trailing spaces
    aws_access_key_id=os.getenv("ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("SECRET_ACCESS_KEY"),
    config=Config(
        retries={'max_attempts': 3},
        connect_timeout=60,
        read_timeout=60
    )
)

S3_BUCKET = os.getenv("S3_BUCKET")

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
        
        # Verify file exists and is readable
        if not os.path.exists(file_path):
            raise Exception(f"File does not exist: {file_path}")
        
        file_size = os.path.getsize(file_path)
        logger.info(f"File size for upload: {file_size} bytes")
        
        if file_size == 0:
            raise Exception("File is empty")
        
        # Upload with additional parameters
        s3.upload_file(
            file_path, 
            S3_BUCKET, 
            s3_key,
            ExtraArgs={'ContentType': 'application/pdf'}
        )
        logger.info(f"Successfully uploaded {s3_key} to S3 bucket {S3_BUCKET}")
        return True
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to upload file to storage: {str(e)}")

def download_from_s3(s3_key: str, local_path: str):
    try:
        logger.info(f"Attempting to download {s3_key} from S3 bucket {S3_BUCKET}")
        s3.download_file(S3_BUCKET, s3_key, local_path)
        logger.info(f"Downloaded {s3_key} from S3 to {local_path}")
        return True
    except Exception as e:
        logger.error(f"Error downloading from S3: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to download file from storage: {str(e)}")

def delete_from_s3(s3_key: str):
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"Deleted {s3_key} from S3")
        return True
    except Exception as e:
        logger.error(f"Error deleting from S3: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete file from storage.")

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
    logger.info(f"Starting add-plan for course_index: {course_index}, name: {name}")
    
    if file:
        logger.info(f"File provided: {file.filename}, content_type: {file.content_type}")
        # Check file type
        if file.content_type != "application/pdf":
            logger.error(f"Invalid file type: {file.content_type}")
            raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files allowed.")
        
        # Check file size (increased to 10MB)
        try:
            contents = await file.read()
            logger.info(f"File size: {len(contents)} bytes")
            if len(contents) > 10 * 1024 * 1024:  # 10MB limit
                logger.error(f"File too large: {len(contents)} bytes")
                raise HTTPException(status_code=413, detail="File size exceeds 10MB limit")
            
            # Reset file pointer
            await file.seek(0)
        except Exception as e:
            logger.error(f"Error reading file: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Error reading file: {str(e)}")

    courses = get_courses()
    logger.info(f"Retrieved courses, count: {len(courses)}")
    
    if 0 <= course_index < len(courses):
        try:
            if file and file.filename:
                filename = sanitize_filename(file.filename)
                logger.info(f"Sanitized filename: {filename}")
                
                # Save file temporarily
                temp_pdf_path = os.path.join(temp_dir, filename)
                logger.info(f"Saving to temp path: {temp_pdf_path}")
                
                file_content = await file.read()
                with open(temp_pdf_path, "wb") as f:
                    f.write(file_content)
                
                # Verify file was saved
                if not os.path.exists(temp_pdf_path):
                    logger.error(f"Failed to save temp file: {temp_pdf_path}")
                    raise HTTPException(status_code=500, detail="Failed to save temporary file")
                
                logger.info(f"Temp file saved, size: {os.path.getsize(temp_pdf_path)} bytes")
                
                # Upload to S3
                logger.info(f"Uploading to S3 bucket: {S3_BUCKET}, key: {filename}")
                upload_to_s3(temp_pdf_path, filename)
                
                # Clean up temp file
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
                    logger.info(f"Cleaned up temp file: {temp_pdf_path}")
                
                courses[course_index]["plans"].append({"name": name, "filename": filename})
                logger.info(f"Added plan to course, plan count: {len(courses[course_index]['plans'])}")
            else:
                # Handle case where no file is uploaded
                courses[course_index]["plans"].append({"name": name, "filename": None})
                logger.info("Added plan without file")
            
            save_courses(courses)
            logger.info("Courses saved successfully")
            return RedirectResponse(url="/admin", status_code=303)
        except Exception as e:
            logger.error(f"Error in add_plan: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Failed to add plan: {str(e)}")
    else:
        logger.error(f"Invalid course index: {course_index}, courses length: {len(courses)}")
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
                # Check file type
                if file.content_type != "application/pdf":
                    raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files allowed.")
                
                # Check file size
                contents = await file.read()
                if len(contents) > 10 * 1024 * 1024:  # 10MB limit
                    raise HTTPException(status_code=413, detail="File size exceeds 10MB limit")
                
                # Reset file pointer
                await file.seek(0)
                
                # Delete old file from S3 if it exists
                old_filename = courses[course_index]["plans"][plan_index]["filename"]
                if old_filename:
                    try:
                        delete_from_s3(old_filename)
                    except Exception as e:
                        logger.error(f"Error deleting old file {old_filename} from S3: {e}")
                
                # Upload new file
                filename = sanitize_filename(file.filename)
                temp_pdf_path = os.path.join(temp_dir, filename)
                with open(temp_pdf_path, "wb") as f:
                    f.write(await file.read())
                
                upload_to_s3(temp_pdf_path, filename)
                os.remove(temp_pdf_path)
                
                courses[course_index]["plans"][plan_index]["filename"] = filename
            
            save_courses(courses)
        except Exception as e:
            logger.error(f"Error editing plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to edit plan")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")

# FIXED: Removed duplicate route definitions - keeping only one download route
@app.get("/download/{filename}")
async def download_pdf(filename: str):
    try:
        logger.info(f"Download request for filename: {filename}")
        
        # Validate filename
        if not filename or filename == "None":
            logger.error("Invalid filename provided")
            raise HTTPException(status_code=400, detail="Invalid filename")
        
        # Option 1: Direct redirect to presigned URL (recommended)
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": filename},
                ExpiresIn=3600,
            )
            logger.info(f"Generated presigned URL for {filename}")
            return RedirectResponse(url=url)
        except Exception as e:
            logger.error(f"Presigned URL generation failed: {e}")
            # Fallback to direct download
            
        # Option 2: Download and serve file (fallback)
        local_path = os.path.join(temp_dir, filename)
        logger.info(f"Downloading {filename} to {local_path}")
        download_from_s3(filename, local_path)
        
        # Verify file exists and has content
        if not os.path.exists(local_path):
            logger.error(f"Downloaded file does not exist: {local_path}")
            raise HTTPException(status_code=404, detail="File not found after download")
        
        file_size = os.path.getsize(local_path)
        logger.info(f"Downloaded file size: {file_size} bytes")
        
        if file_size == 0:
            logger.error("Downloaded file is empty")
            raise HTTPException(status_code=404, detail="Downloaded file is empty")
        
        response = FileResponse(
            local_path, 
            media_type="application/pdf", 
            filename=filename
        )
        
        # Clean up temp file after response is sent
        @response.on_close
        def cleanup_temp_file():
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
                    logger.info(f"Cleaned up temp file: {local_path}")
            except Exception as e:
                logger.error(f"Error cleaning up temp file: {e}")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download failed: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")

@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        # Delete all associated files from S3
        for plan in courses[course_index]["plans"]:
            if plan["filename"]:  # Only delete if filename exists
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
        # Delete file from S3 if it exists
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
    elif exc.status_code == 413:  # Handle "Request Entity Too Large"
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
    logger.error(f"Traceback: {traceback.format_exc()}")
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
