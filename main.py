from fastapi import FastAPI, Form, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import json

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Hardcoded admin credentials
ADMIN_EMAIL = "admin@site.com"
ADMIN_PASSWORD = "password"
PORT= 9216

# Simulate session storage
is_logged_in = False

class Plan(BaseModel):
    name: str
    pdf_url: str

class Course(BaseModel):
    title: str
    plans: list[Plan] = []

courses = []

@app.get("/")
async def user_dashboard(request: Request):
    return templates.TemplateResponse("user_dashboard.html", {"request": request, "courses": courses})

@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    global is_logged_in
    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        is_logged_in = True
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

@app.get("/admin")
async def admin_dashboard(request: Request):
    if not is_logged_in:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})

@app.post("/add-course")
async def add_course(title: str = Form(...)):
    courses.append(Course(title=title, plans=[]))
    return RedirectResponse("/admin", status_code=303)

@app.post("/add-plan")
async def add_plan(course_index: int = Form(...), plan_name: str = Form(...), pdf_url: str = Form(...)):
    if 0 <= course_index < len(courses):
        courses[course_index].plans.append(Plan(name=plan_name, pdf_url=pdf_url))
    return RedirectResponse("/admin", status_code=303)

@app.post("/delete-course")
async def delete_course(course_index: int = Form(...)):
    if 0 <= course_index < len(courses):
        courses.pop(course_index)
    return RedirectResponse("/admin", status_code=303)

@app.post("/delete-plan")
async def delete_plan(course_index: int = Form(...), plan_index: int = Form(...)):
    if 0 <= course_index < len(courses) and 0 <= plan_index < len(courses[course_index].plans):
        courses[course_index].plans.pop(plan_index)
    return RedirectResponse("/admin", status_code=303)

@app.post("/update-course-order")
async def update_course_order(new_order: str = Form(...)):
    order = json.loads(new_order)
    reordered_courses = [courses[i] for i in order]
    courses.clear()
    courses.extend(reordered_courses)
    return RedirectResponse("/admin", status_code=303)

@app.post("/update-plan-order")
async def update_plan_order(course_index: int = Form(...), new_order: str = Form(...)):
    if 0 <= course_index < len(courses):
        order = json.loads(new_order)
        reordered_plans = [courses[course_index].plans[i] for i in order]
        courses[course_index].plans.clear()
        courses[course_index].plans.extend(reordered_plans)
    return RedirectResponse("/admin", status_code=303)
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
