import os
import shutil
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, File, UploadFile, Request, HTTPException, Body, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from celery.result import AsyncResult

from celery_worker import celery_app, ocr_single_image_task, process_and_generate_merged_pdf_task

import pytesseract
try:
    pytesseract.get_tesseract_version()
except pytesseract.TesseractNotFoundError:
    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

UPLOAD_DIR = Path("uploads")
PDF_DIR = Path("generated_pdfs")
STATIC_DIR = Path("static")
UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="templates")

# ------------------------- PYDANTIC MODELS -----------------------------
class OCRPageInfo(BaseModel):
    temp_filename: str
    original_filename: str

class OCRMultiplePagesPayload(BaseModel):
    pages_to_ocr: List[OCRPageInfo]
    lang: str

class FileInfo(BaseModel): 
    temp_filename: str
    original_filename: str
    lang: str

class GeneratePdfPayload(BaseModel):
    files_to_process: List[FileInfo]

# ------------------------ API ENDPOINTS ---------------------------

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index_alpine.html", {"request": request})

@app.post("/upload/", response_class=JSONResponse)
async def upload_images(files: List[UploadFile] = File(...)):
    uploaded_files_info = []
    for file in files:
        if not file.content_type or not file.content_type.startswith("image/"): continue
        file_suffix = Path(file.filename).suffix.lower() or '.png'
        temp_filename = f"{uuid.uuid4()}{file_suffix}"
        temp_filepath = UPLOAD_DIR / temp_filename
        try:
            with open(temp_filepath, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
            uploaded_files_info.append({"original_filename": file.filename, "temp_filename": temp_filename})
        finally:
            if hasattr(file, 'file') and not file.file.closed: await file.close()
    return JSONResponse(content={"uploaded_files": uploaded_files_info})

@app.post("/ocr-multiple-pages/", response_class=JSONResponse)
async def ocr_multiple_pages_async(payload: OCRMultiplePagesPayload):
    submitted_tasks = []
    for page_info in payload.pages_to_ocr:
        image_path = UPLOAD_DIR / page_info.temp_filename
        if image_path.is_file():
            try:
                task = ocr_single_image_task.delay(
                    image_path_str=str(image_path),
                    lang=payload.lang,
                    temp_filename=page_info.temp_filename
                )
                submitted_tasks.append({"temp_filename": page_info.temp_filename, "task_id": task.id})
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not submit OCR task: {e}")
    return {"message": f"Submitted {len(submitted_tasks)} OCR tasks.", "submitted_tasks": submitted_tasks}

@app.post("/generate-book-pdf-async/", response_class=JSONResponse)
async def generate_book_pdf_async_endpoint(payload: GeneratePdfPayload):
    files_to_process_pydantic = payload.files_to_process
    if not files_to_process_pydantic:
        raise HTTPException(status_code=400, detail="No files specified for PDF generation.")

    files_to_process_plain = [item.dict() for item in files_to_process_pydantic]
    
    try:
        task = process_and_generate_merged_pdf_task.delay(files_to_process_plain)
        return {"message": "PDF generation task submitted.", "task_id": task.id}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Could not submit PDF task to queue: {e}")

@app.get("/task-status/{task_id}", response_class=JSONResponse)
async def get_task_status(task_id: str):
    task_result = celery_app.AsyncResult(task_id)
    response_data = {"task_id": task_id, "status": task_result.status, "result": None}
    if task_result.successful():
        response_data["result"] = task_result.get()
    elif task_result.failed():
        error_info = task_result.result
        if isinstance(error_info, Exception):
            response_data["result"] = {"error": str(error_info)}
        else:
            response_data["result"] = error_info
    return response_data

@app.get("/download-generated-pdf/{filename}", response_class=FileResponse)
async def download_generated_pdf(filename: str):
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    file_path = PDF_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Generated PDF file not found or already cleaned up.")
    
    return FileResponse(path=file_path, filename=filename, media_type='application/pdf')