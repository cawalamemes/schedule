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
from dotenv import load_dotenv
from pathlib import Path
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_404_NOT_FOUND
import logging

load_dotenv()

app = FastAPI()

static_dir = os.path.abspath("static")
pdfs_dir = os.path.join(static_dir, "pdfs")
os.makedirs(pdfs_dir, exist_ok=True)

LOG_FILE = "logs.txt"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
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

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type")
    if file.size > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds limit")

    courses = get_courses()
    if 0 <= course_index < len(courses):
        try:
            filename = sanitize_filename(file.filename)
            pdf_path = os.path.join(pdfs_dir, filename)
            with open(pdf_path, "wb") as f:
                f.write(await file.read())
            courses[course_index]["plans"].append({"name": name, "filename": filename})
            save_courses(courses)
        except Exception as e:
            logger.error(f"Error adding plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to add plan")
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")

@app.get("/download/{filename}")
async def download_pdf(filename: str):
    pdf_path = os.path.join(pdfs_dir, filename)
    if not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

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
    return FileResponse(LOG_FILE, media_type="text/plain", filename="logs.txt")
            
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return HTMLResponse(content="An unexpected error occurred", status_code=500)
