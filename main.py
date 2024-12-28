from fastapi import FastAPI, Form, UploadFile, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List
import os
import json
from bson import ObjectId
from pymongo import MongoClient

# MongoDB Configuration
client = MongoClient("mongodb+srv://itsharshit:5hiOsDJBc0sihKCc@mcs.o129f.mongodb.net/?retryWrites=true&w=majority&appName=mcs")
db = client["course_app"]
courses_collection = db["courses"]

app = FastAPI()

PORT = 9216

# Static and Template Directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    courses = list(courses_collection.find({}, {"_id": 0}))  # Fetch all courses
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
    courses = list(courses_collection.find({}, {"_id": 0}))
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "courses": courses})


@app.post("/add-course")
async def add_course(title: str = Form(...)):
    new_course = {"title": title, "plans": []}
    courses_collection.insert_one(new_course)  # Insert new course into MongoDB
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/add-plan")
async def add_plan(course_id: str = Form(...), name: str = Form(...), file: UploadFile = None):
    pdf_path = f"static/pdfs/{file.filename}"
    with open(pdf_path, "wb") as f:
        f.write(file.file.read())
    plan = {"name": name, "pdf_url": f"/{pdf_path}"}
    courses_collection.update_one({"_id": ObjectId(course_id)}, {"$push": {"plans": plan}})
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/delete-course")
async def delete_course(course_id: str = Form(...)):
    result = courses_collection.delete_one({"_id": ObjectId(course_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Course not found")
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/delete-plan")
async def delete_plan(course_id: str = Form(...), plan_index: int = Form(...)):
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    if not course or plan_index < 0 or plan_index >= len(course["plans"]):
        raise HTTPException(status_code=404, detail="Plan not found")
    course["plans"].pop(plan_index)
    courses_collection.update_one({"_id": ObjectId(course_id)}, {"$set": {"plans": course["plans"]}})
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/reorder-courses")
async def reorder_courses(request: Request):
    body = await request.form()
    new_order = json.loads(body["new_order"])
    # Update the order field in MongoDB if needed, or reorder client-side
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/reorder-plans")
async def reorder_plans(request: Request):
    body = await request.form()
    course_id = body["course_id"]
    new_order = json.loads(body["new_order"])
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    if not course or len(new_order) != len(course["plans"]):
        raise HTTPException(status_code=400, detail="Invalid order")
    reordered_plans = [course["plans"][i] for i in new_order]
    courses_collection.update_one({"_id": ObjectId(course_id)}, {"$set": {"plans": reordered_plans}})
    return RedirectResponse(url="/admin", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
