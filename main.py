from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
from passlib.context import CryptContext
import os
import json
import redis
from dotenv import load_dotenv
from uuid import uuid4

load_dotenv()

app = FastAPI()

# Ensure static directory exists
if not os.path.exists("static"):
    os.makedirs("static")
if not os.path.exists("static/pdfs"):
    os.makedirs("static/pdfs")
PORT = 8000

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Redis Configuration
REDIS = os.getenv("REDIS_URI")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

REDIS_URI = REDIS.split(":")
host = REDIS_URI[0]
port = int(REDIS_URI[1])
pass_word = REDIS_PASSWORD

redis_client = redis.StrictRedis(
    host=host,
    port=port,
    password=pass_word,
    decode_responses=True
)

try:
    redis_client.ping()
    print("Connected to Redis!")
except redis.AuthenticationError:
    print("Authentication failed: Check your password")
except redis.ConnectionError:
    print("Connection error: Check your host and port")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Admin Credentials
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = pwd_context.hash(os.getenv("ADMIN_PASSWORD"))


# Helper Functions
def get_courses():
    courses_json = redis_client.get("courses")
    return json.loads(courses_json) if courses_json else []


def save_courses(courses):
    redis_client.set("courses", json.dumps(courses, default=lambda o: o.dict()))


def create_session():
    session_id = str(uuid4())
    redis_client.setex(f"session:{session_id}", 3600, "logged_in")
    return session_id


def is_logged_in(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    return redis_client.get(f"session:{session_id}") == "logged_in"


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
        # Remove session from Redis
        redis_client.delete(f"session:{session_id}")
        print(f"Session {session_id} deleted.")
    # Clear the session cookie
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
    courses = get_courses()
    if 0 <= course_index < len(courses):
        pdf_path = f"static/pdfs/{file.filename}"
        with open(pdf_path, "wb") as f:
            f.write(file.file.read())
        courses[course_index]["plans"].append({"name": name, "pdf_url": f"/{pdf_path}"})
        save_courses(courses)
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=404, detail="Course not found")
    
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
