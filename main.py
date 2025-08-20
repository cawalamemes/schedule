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

# Debug endpoints
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
        # Test specific bucket access
        try:
            bucket_response = s3.list_objects_v2(Bucket=S3_BUCKET, MaxKeys=100)
            files = []
            if 'Contents' in bucket_response:
                files = [obj['Key'] for obj in bucket_response['Contents']]
            logger.info(f"Bucket {S3_BUCKET} access successful. Files: {len(files)}")
            return {
                "status": "success",
                "bucket": S3_BUCKET,
                "files": files[:10],
                "total_files": len(files)
            }
        except Exception as bucket_error:
            logger.error(f"Bucket access failed: {bucket_error}")
            return {"status": "bucket_error", "error": str(bucket_error), "bucket": S3_BUCKET}
            
    except Exception as e:
        logger.error(f"S3 test failed: {str(e)}")
        return {"status": "connection_error", "error": str(e)}

@app.get("/debug-courses")
async def debug_courses():
    try:
        courses = get_courses()
        s3_files = []
        try:
            response = s3.list_objects_v2(Bucket=S3_BUCKET)
            if 'Contents' in response:
                s3_files = [obj['Key'] for obj in response['Contents']]
        except Exception as e:
            logger.error(f"Error listing S3 files: {e}")
            
        # Find mismatched files
        mismatched_files = []
        for i, course in enumerate(courses):
            for j, plan in enumerate(course.get("plans", [])):
                filename = plan.get("filename")
                if filename and filename not in s3_files:
                    mismatched_files.append({
                        "course_index": i,
                        "plan_index": j,
                        "course_title": course.get("title", "Unknown"),
                        "plan_name": plan.get("name", "Unknown"),
                        "filename": filename
                    })
        
        return {
            "courses": courses,
            "s3_files": s3_files,
            "s3_file_count": len(s3_files),
            "bucket": S3_BUCKET,
            "mismatched_files": mismatched_files,
            "mismatched_count": len(mismatched_files)
        }
    except Exception as e:
        logger.error(f"Debug courses failed: {str(e)}")
        return {"error": str(e)}

@app.get("/fix-mismatched-files")
async def fix_mismatched_files():
    """Remove references to files that don't exist in S3"""
    try:
        courses = get_courses()
        s3_files = []
        try:
            response = s3.list_objects_v2(Bucket=S3_BUCKET)
            if 'Contents' in response:
                s3_files = [obj['Key'] for obj in response['Contents']]
        except Exception as e:
            logger.error(f"Error listing S3 files: {e}")
            return {"error": "Could not list S3 files"}
        
        fixed_count = 0
        # Remove references to non-existent files
        for course in courses:
            plans_to_remove = []
            for i, plan in enumerate(course.get("plans", [])):
                filename = plan.get("filename")
                if filename and filename not in s3_files:
                    logger.info(f"Removing reference to non-existent file: {filename}")
                    plans_to_remove.append(i)
                    fixed_count += 1
            
            # Remove plans in reverse order to maintain indices
            for i in reversed(plans_to_remove):
                course["plans"].pop(i)
        
        if fixed_count > 0:
            save_courses(courses)
            logger.info(f"Fixed {fixed_count} mismatched file references")
            return {"status": "success", "fixed_count": fixed_count}
        else:
            return {"status": "success", "message": "No mismatched files found"}
            
    except Exception as e:
        logger.error(f"Fix mismatched files failed: {str(e)}")
        return {"error": str(e)}

@app.get("/test-upload")
async def test_upload():
    try:
        # Create a small test file
        test_filename = "test_file_" + uuid.uuid4().hex[:6] + ".txt"
        test_content = "This is a test file for upload testing."
        temp_path = os.path.join("/tmp", test_filename)
        
        # Write test file
        with open(temp_path, "w") as f:
            f.write(test_content)
        
        logger.info(f"Created test file: {temp_path}, size: {os.path.getsize(temp_path)} bytes")
        
        # Upload to S3
        logger.info(f"Uploading test file to S3: {S3_BUCKET}/{test_filename}")
        s3.upload_file(temp_path, S3_BUCKET, test_filename)
        logger.info("Test file uploaded successfully")
        
        # Verify upload
        try:
            response = s3.head_object(Bucket=S3_BUCKET, Key=test_filename)
            logger.info(f"Test file verified in S3. Size: {response.get('ContentLength', 'unknown')} bytes")
        except Exception as verify_error:
            logger.error(f"Failed to verify test file: {verify_error}")
        
        # Clean up local file
        os.remove(temp_path)
        logger.info("Cleaned up local test file")
        
        return {"status": "success", "filename": test_filename, "content": test_content}
        
    except Exception as e:
        logger.error(f"Test upload failed: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {"status": "error", "message": str(e)}

# Configure S3 client with retry configuration
s3 = boto3.client(
    "s3",
    region_name="us-east-1",
    endpoint_url="https://objstorage.leapcell.io",
    aws_access_key_id=os.getenv("KEY_ID"),
    aws_secret_access_key=os.getenv("SECRET_ACCESS_KEY"),
    config=Config(
        retries={'max_attempts': 3, 'mode': 'adaptive'},
        connect_timeout=10,
        read_timeout=30
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
    socket_connect_timeout=5,
    socket_timeout=5,
    retry_on_timeout=True
)

try:
    redis_client.ping()
    logger.info("Connected to Redis!")
except redis.AuthenticationError:
    logger.error("Authentication failed")
except redis.ConnectionError:
    logger.error("Connection error")
except Exception as e:
    logger.error(f"Redis connection error: {e}")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = pwd_context.hash(os.getenv("ADMIN_PASSWORD"))

def sanitize_filename(filename: str) -> str:
    if not filename:
        return "unnamed_file_" + uuid.uuid4().hex[:6] + ".pdf"
    
    path = Path(filename)
    stem = path.stem
    extension = path.suffix.lower()
    
    # If no extension or not PDF, default to .pdf
    if not extension or extension not in ['.pdf']:
        extension = '.pdf'
    
    stem = re.sub(r'[^a-zA-Z0-9_-]', '', stem.replace(' ', '_'))
    stem = re.sub(r'_+', '_', stem).strip('_')
    if not stem:
        stem = "file"
    unique_id = uuid.uuid4().hex[:6]
    sanitized_name = f"{stem}_{unique_id}{extension}"
    logger.info(f"Sanitized '{filename}' to '{sanitized_name}'")
    return sanitized_name

def get_courses():
    try:
        courses_json = redis_client.get("courses")
        courses = json.loads(courses_json) if courses_json else []
        logger.info(f"Retrieved {len(courses)} courses from Redis")
        return courses
    except Exception as e:
        logger.error(f"Error fetching courses from Redis: {e}")
        return []

def save_courses(courses):
    try:
        redis_client.set("courses", json.dumps(courses))
        logger.info(f"Saved {len(courses)} courses to Redis")
    except Exception as e:
        logger.error(f"Error saving courses to Redis: {e}")
        raise HTTPException(status_code=500, detail="Failed to save courses.")

def create_session():
    session_id = str(uuid.uuid4())
    try:
        redis_client.setex(f"session:{session_id}", 3600, "logged_in")
        logger.info(f"Created session: {session_id}")
    except Exception as e:
        logger.error(f"Error creating session: {e}")
    return session_id

def is_logged_in(session_id: Optional[str]) -> bool:
    try:
        if not session_id:
            return False
        result = redis_client.get(f"session:{session_id}")
        is_valid = result == "logged_in"
        logger.info(f"Session check for {session_id}: {is_valid}")
        return is_valid
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
            ExtraArgs={
                'ContentType': 'application/pdf',
                'Metadata': {
                    'uploaded-by': 'course-management-app',
                    'upload-timestamp': str(uuid.uuid4().hex[:8])
                }
            }
        )
        logger.info(f"Successfully uploaded {s3_key} to S3 bucket {S3_BUCKET}")
        
        # Verify upload with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
                logger.info(f"Verified upload of {s3_key}. Size: {response.get('ContentLength', 'unknown')} bytes")
                return True
            except Exception as verify_error:
                logger.warning(f"Upload verification attempt {attempt + 1} failed: {verify_error}")
                if attempt == max_retries - 1:
                    raise Exception(f"Upload verification failed after {max_retries} attempts: {verify_error}")
                import time
                time.sleep(1)
            
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to upload file to storage: {str(e)}")

def download_from_s3(s3_key: str, local_path: str):
    try:
        logger.info(f"Starting S3 download: {S3_BUCKET}/{s3_key} -> {local_path}")
        
        # Check if object exists first
        try:
            response = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
            logger.info(f"Object {s3_key} exists in bucket {S3_BUCKET}, size: {response.get('ContentLength', 'unknown')} bytes")
        except s3.exceptions.NoSuchKey:
            logger.error(f"Object {s3_key} not found in bucket {S3_BUCKET}")
            raise Exception(f"File '{s3_key}' not found in storage. This file may have been deleted or never uploaded successfully.")
        except Exception as e:
            logger.error(f"Error checking object existence: {str(e)}")
            raise Exception(f"Error accessing file in storage: {str(e)}")
        
        # Download the file
        s3.download_file(S3_BUCKET, s3_key, local_path)
        logger.info(f"Successfully downloaded {s3_key} from S3 to {local_path}")
        
        # Verify file was downloaded
        if not os.path.exists(local_path):
            raise Exception(f"File was not downloaded to {local_path}")
            
        file_size = os.path.getsize(local_path)
        logger.info(f"Downloaded file size: {file_size} bytes")
        
    except Exception as e:
        logger.error(f"Error downloading from S3 key {s3_key}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise Exception(f"Failed to download file from storage: {str(e)}")

def delete_from_s3(s3_key: str):
    try:
        # Check if file exists before deleting
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
            logger.info(f"File {s3_key} exists, proceeding with deletion")
        except s3.exceptions.NoSuchKey:
            logger.warning(f"File {s3_key} not found in S3, skipping deletion")
            return True
        except Exception as e:
            logger.warning(f"Error checking file existence before deletion: {e}")
        
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"Deleted {s3_key} from S3")
        return True
    except Exception as e:
        logger.error(f"Error deleting from S3: {e}")
        return False

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
        try:
            redis_client.delete(f"session:{session_id}")
            logger.info(f"Deleted session: {session_id}")
        except Exception as e:
            logger.error(f"Error deleting session: {e}")
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
    logger.info(f"=== STARTING ADD-PLAN ===")
    logger.info(f"Course index: {course_index}, Plan name: {name}")
    
    if file:
        logger.info(f"File provided: {file.filename}")
        logger.info(f"File content type: {file.content_type}")
        
        # Check file type
        if file.content_type != "application/pdf":
            logger.error(f"Invalid file type: {file.content_type}")
            raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files allowed.")
        
        # Read file content to check size
        try:
            contents = await file.read()
            logger.info(f"Actual file size: {len(contents)} bytes")
            if len(contents) > 10 * 1024 * 1024:  # 10MB limit
                logger.error(f"File too large: {len(contents)} bytes")
                raise HTTPException(status_code=413, detail="File size exceeds 10MB limit")
            
            # Reset file pointer
            await file.seek(0)
        except Exception as e:
            logger.error(f"Error reading file: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Error reading file: {str(e)}")
    else:
        logger.info("No file provided")

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
                
                file_size = os.path.getsize(temp_pdf_path)
                logger.info(f"Temp file saved, size: {file_size} bytes")
                
                if file_size == 0:
                    logger.error("Temp file is empty!")
                    os.remove(temp_pdf_path)  # Clean up
                    raise HTTPException(status_code=500, detail="Uploaded file is empty")
                
                # Upload to S3
                logger.info(f"Uploading to S3 bucket: {S3_BUCKET}, key: {filename}")
                upload_success = upload_to_s3(temp_pdf_path, filename)
                
                # Clean up temp file immediately after upload attempt
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
                    logger.info(f"Cleaned up temp file: {temp_pdf_path}")
                
                if upload_success:
                    courses[course_index]["plans"].append({"name": name, "filename": filename})
                    logger.info(f"Added plan to course, plan count: {len(courses[course_index]['plans'])}")
                else:
                    raise HTTPException(status_code=500, detail="Failed to upload file to storage")
            else:
                # Handle case where no file is uploaded
                courses[course_index]["plans"].append({"name": name, "filename": None})
                logger.info("Added plan without file")
            
            save_courses(courses)
            logger.info("Courses saved successfully")
            logger.info("=== FINISHED ADD-PLAN SUCCESSFULLY ===")
            return RedirectResponse(url="/admin", status_code=303)
        except Exception as e:
            logger.error(f"Error in add_plan: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.info("=== FINISHED ADD-PLAN WITH ERROR ===")
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
                        logger.info(f"Deleted old file: {old_filename}")
                    except Exception as e:
                        logger.error(f"Error deleting old file {old_filename} from S3: {e}")
                
                # Upload new file
                filename = sanitize_filename(file.filename)
                temp_pdf_path = os.path.join(temp_dir, filename)
                
                # Save new file
                with open(temp_pdf_path, "wb") as f:
                    f.write(await file.read())
                
                # Upload to S3
                upload_success = upload_to_s3(temp_pdf_path, filename)
                
                # Clean up temp file
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
                
                if upload_success:
                    courses[course_index]["plans"][plan_index]["filename"] = filename
                    logger.info(f"Updated plan with new file: {filename}")
                else:
                    raise HTTPException(status_code=500, detail="Failed to upload new file to storage")
            
            save_courses(courses)
            logger.info("Courses updated successfully")
            return RedirectResponse(url="/admin", status_code=303)
        except Exception as e:
            logger.error(f"Error editing plan: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Failed to edit plan: {str(e)}")
    raise HTTPException(status_code=404, detail="Plan not found")

@app.get("/download/{filename}")
async def download_pdf(filename: str):
    logger.info(f"=== STARTING DOWNLOAD ===")
    logger.info(f"Requested filename: {filename}")
    
    # Validate filename
    if not filename or ".." in filename or "/" in filename:
        logger.error(f"Invalid filename: {filename}")
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    # Create temp file path
    temp_pdf_path = os.path.join(temp_dir, filename)
    logger.info(f"Temp path for download: {temp_pdf_path}")
    
    try:
        # Download from S3 to temp location
        logger.info(f"Downloading from S3 bucket: {S3_BUCKET}, key: {filename}")
        download_from_s3(filename, temp_pdf_path)
        
        # Verify file was downloaded
        if not os.path.exists(temp_pdf_path):
            logger.error(f"File not found after S3 download: {temp_pdf_path}")
            raise HTTPException(status_code=404, detail="File not found after download")
        
        file_size = os.path.getsize(temp_pdf_path)
        logger.info(f"Downloaded file size: {file_size} bytes")
        
        if file_size == 0:
            logger.error(f"Downloaded file is empty: {temp_pdf_path}")
            # Clean up empty file
            if os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)
            raise HTTPException(status_code=500, detail="Downloaded file is empty")
        
        # Return file response
        response = FileResponse(
            temp_pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
        logger.info(f"File response created for: {filename}")
        
        # Clean up temp file after response is sent
        @response.on_close
        def cleanup_temp_file():
            try:
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
                    logger.info(f"Cleaned up temp file: {temp_pdf_path}")
            except Exception as e:
                logger.error(f"Error cleaning up temp file {temp_pdf_path}: {e}")
        
        logger.info("=== DOWNLOAD COMPLETED SUCCESSFULLY ===")
        return response
        
    except HTTPException:
        # Re-raise HTTP exceptions
        logger.error("=== DOWNLOAD FAILED WITH HTTP EXCEPTION ===")
        raise
    except Exception as e:
        logger.error(f"Error downloading file {filename}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.info("=== DOWNLOAD FAILED WITH GENERAL EXCEPTION ===")
        # Clean up temp file if it exists
        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
                logger.info(f"Cleaned up temp file after error: {temp_pdf_path}")
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up temp file after download error: {cleanup_error}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}. The file may not exist in storage. Visit /debug-courses to check file consistency, or /fix-mismatched-files to clean up references.")

@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        # Delete all associated files from S3
        for plan in courses[course_index]["plans"]:
            if plan["filename"]:  # Only delete if filename exists
                try:
                    delete_result = delete_from_s3(plan["filename"])
                    if delete_result:
                        logger.info(f"Deleted file from S3: {plan['filename']}")
                    else:
                        logger.warning(f"Failed to delete file from S3: {plan['filename']}")
                except Exception as e:
                    logger.error(f"Error deleting file {plan['filename']} from S3: {e}")
        
        courses.pop(course_index)
        save_courses(courses)
        logger.info(f"Deleted course at index {course_index}")
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
                delete_result = delete_from_s3(filename)
                if delete_result:
                    logger.info(f"Deleted file from S3: {filename}")
                else:
                    logger.warning(f"Failed to delete file from S3: {filename}")
            except Exception as e:
                logger.error(f"Error deleting file {filename} from S3: {e}")
        
        courses[course_index]["plans"].pop(plan_index)
        save_courses(courses)
        logger.info(f"Deleted plan at index {plan_index} from course {course_index}")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.error(f"HTTP Exception: {exc.status_code} - {exc.detail}")
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
        limit_concurrency=100,
        limit_max_requests=1000,
    )
