from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List
from pydantic import BaseModel
import os
import json

app = FastAPI()

PORT= 9216

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory Database
courses = []

admin_credentials = {"email": "admin@site.com", "password": "password"}
admin_logged_in = False

class Plan(BaseModel):
    name: str
    pdf_url: str

class Course(BaseModel):
    title: str
    plans: List[Plan] = []

@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login_post(email: str = Form(...), password: str = Form(...)):
    global admin_logged_in
    if email == admin_credentials["email"] and password == admin_credentials["password"]:
        admin_logged_in = True
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not admin_logged_in:
        return RedirectResponse(url="/admin/login", status_code=303)
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})

@app.post("/add-course")
async def add_course(title: str = Form(...)):
    courses.append(Course(title=title, plans=[]))
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    pdf_path = f"static/pdfs/{file.filename}"
    with open(pdf_path, "wb") as f:
        f.write(file.file.read())
    courses[course_index].plans.append(Plan(name=name, pdf_url=f"/{pdf_path}"))
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/reorder-courses")
async def reorder_courses(request: Request):
    global courses
    body = await request.form()
    new_order = json.loads(body["new_order"])
    courses = [courses[i] for i in new_order]
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/reorder-plans")
async def reorder_plans(request: Request):
    body = await request.form()
    course_index = int(body["course_index"])
    new_order = json.loads(body["new_order"])
    courses[course_index].plans = [courses[course_index].plans[i] for i in new_order]
    return RedirectResponse(url="/admin", status_code=303)

from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List
from pydantic import BaseModel
import os
import json

app = FastAPI()

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory Database
courses = []

admin_credentials = {"email": "admin@site.com", "password": "password"}
admin_logged_in = False

class Plan(BaseModel):
    name: str
    pdf_url: str

class Course(BaseModel):
    title: str
    plans: List[Plan] = []

@app.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login_post(email: str = Form(...), password: str = Form(...)):
    global admin_logged_in
    if email == admin_credentials["email"] and password == admin_credentials["password"]:
        admin_logged_in = True
        return RedirectResponse(url="/admin", status_code=303)
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not admin_logged_in:
        return RedirectResponse(url="/admin/login", status_code=303)
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})

@app.post("/add-course")
async def add_course(title: str = Form(...)):
    courses.append(Course(title=title, plans=[]))
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...), name: str = Form(...), file: UploadFile = None):
    pdf_path = f"static/pdfs/{file.filename}"
    with open(pdf_path, "wb") as f:
        f.write(file.file.read())
    courses[course_index].plans.append(Plan(name=name, pdf_url=f"/{pdf_path}"))
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/reorder-courses")
async def reorder_courses(request: Request):
    global courses
    body = await request.form()
    new_order = json.loads(body["new_order"])
    courses = [courses[i] for i in new_order]
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/reorder-plans")
async def reorder_plans(request: Request):
    body = await request.form()
    course_index = int(body["course_index"])
    new_order = json.loads(body["new_order"])
    courses[course_index].plans = [courses[course_index].plans[i] for i in new_order]
    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
