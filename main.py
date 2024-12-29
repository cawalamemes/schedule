from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette_session import SessionMiddleware
from starlette_session.backends import SecureCookieBackend
from typing import List
from pydantic import BaseModel
import os
import json
import redis
import secrets

# FastAPI Application
app = FastAPI()

PORT = 9216

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Redis Configuration
REDIS = "redis-19912.crce179.ap-south-1-1.ec2.redns.redis-cloud.com:19912"
REDIS_PASSWORD = "2L7qgMeLou5rezLa6XU2iNIDdG1RSTUq"

REDIS_URI = REDIS.split(":")
host = REDIS_URI[0]
port = int(REDIS_URI[1])
password = REDIS_PASSWORD

redis_client = redis.StrictRedis(
    host=host,
    port=port,
    password=password,
    decode_responses=True  # Ensure response strings are decoded
)

try:
    redis_client.ping()
    print("Connected to Redis!")
except redis.AuthenticationError:
    print("Authentication failed: Check your password")
except redis.ConnectionError:
    print("Connection error: Check your host and port")

# Admin Credentials
admin_credentials = {"email": "admin@site.com", "password": "password"}

# Secret Key for Secure Cookie Backend
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

# Add Session Middleware with SecureCookieBackend
app.add_middleware(SessionMiddleware, backend=SecureCookieBackend(), secret_key=SECRET_KEY)

# Models
class Plan(BaseModel):
    name: str
    pdf_url: str


class Course(BaseModel):
    title: str
    plans: List[Plan] = []


# Helper Functions
def get_courses():
    courses_json = redis_client.get("courses")
    return json.loads(courses_json) if courses_json else []


def save_courses(courses):
    redis_client.set("courses", json.dumps(courses, default=lambda o: o.dict()))


@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    courses = get_courses()
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
async def admin_login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    if email == admin_credentials["email"] and password == admin_credentials["password"]:
        request.session["admin_logged_in"] = True
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not request.session.get("admin_logged_in"):
        return RedirectResponse(url="/admin/login", status_code=303)
    courses = get_courses()
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
