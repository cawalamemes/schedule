from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from typing import Optional
import os
import json
import redis
from dotenv import load_dotenv
from uuid import uuid4
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_404_NOT_FOUND
import logging
from urllib.parse import unquote

# Load environment variables
load_dotenv()

# Initialize FastAPI
app = FastAPI()

# Ensure static directories exist
if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("static/pdfs"):
    os.makedirs("static/pdfs")

# Logging configuration
LOG_FILE = "logs.txt"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Redis Configuration
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
    logger.error("Authentication failed: Check your password")
except redis.ConnectionError:
    logger.error("Connection error: Check your host and port")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Admin Credentials
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = pwd_context.hash(os.getenv("ADMIN_PASSWORD"))

# Helper Functions
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
    session_id = str(uuid4())
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

@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    courses = get_courses()
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})

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
        response.set_cookie(key="session_id", value=session_id, httponly=True, secure=True)
        return response
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session_id: Optional[str] = Cookie(None)
):
    if not is_logged_in(session_id):
        return RedirectResponse(url="/admin/login", status_code=303)
    courses = get_courses()
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})

@app.get("/logout")
async def admin_logout(
    response: RedirectResponse,
    session_id: Optional[str] = Cookie(None)
):
    if session_id:
        redis_client.delete(f"session:{session_id}")
        logger.info(f"Session {session_id} deleted.")
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key="session_id")
    return response

@app.post("/add-course")
async def add_course(title: str = Form(...)):
    courses = get_courses()
    courses.append({"title": title, "plans": []})
    save_courses(courses)
    logger.info(f"Added course: {title}")
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. Only PDFs are allowed.")
    if file.size > 10 * 1024 * 1024:  # Limit file size to 5MB
        raise HTTPException(status_code=400, detail="File size exceeds the 5MB limit.")

    courses = get_courses()
    if 0 <= course_index < len(courses):
        try:
            pdf_path = f"static/pdfs/{file.filename}"
            with open(pdf_path, "wb") as f:
                f.write(file.file.read())
            courses[course_index]["plans"].append({"name": name, "pdf_url": f"/{pdf_path}"})
            save_courses(courses)
        except Exception as e:
            logger.error(f"Error adding plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to add plan.")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found.")


@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses):
        courses.pop(course_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")


@app.post("/delete-plan")
async def delete_plan(course_index: int = Form(...), plan_index: int = Form(...)):
    courses = get_courses()
    if 0 <= course_index < len(courses) and 0 <= plan_index < len(courses[course_index]["plans"]):
        courses[course_index]["plans"].pop(plan_index)
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Plan not found")


@app.post("/update-course-order")
async def update_course_order(request: Request):
    body = await request.form()
    new_order = json.loads(body["new_order"])
    courses = get_courses()
    if len(new_order) == len(courses):
        courses = [courses[int(i)] for i in new_order]
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=400, detail="Invalid course order")

@app.post("/update-plan-order")
async def update_plan_order(request: Request):
    body = await request.form()
    course_index = int(body["course_index"])
    new_order = json.loads(body["new_order"])
    courses = get_courses()
    if 0 <= course_index < len(courses) and len(new_order) == len(courses[course_index]["plans"]):
        courses[course_index]["plans"] = [courses[course_index]["plans"][int(i)] for i in new_order]
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=400, detail="Invalid plan order")

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == HTTP_404_NOT_FOUND:
        # Pass error details to the template
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
        logger.info("Log file created.")
    return FileResponse(LOG_FILE, media_type="text/plain", filename="logs.txt")
            
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return HTMLResponse(content="An unexpected error occurred.", status_code=500)



@app.get("/download-pdf")
async def download_pdf(file_path: str):  # Changed parameter name
    try:
        # Decode and sanitize the path
        decoded_path = unquote(file_path)
        filename = os.path.basename(decoded_path)  # Extract just the filename
        
        # Security checks
        if not filename or '/' in filename or '..' in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        
        # Full path construction
        pdf_path = os.path.join("static", "pdfs", filename)
        
        if not os.path.exists(pdf_path):
            raise HTTPException(status_code=404, detail="File not found")
        
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=filename
        )
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail="Download failed")
