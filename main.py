import os
import shutil
import uuid
import io
from pathlib import Path
from typing import List, Dict, Any, Optional

import pytesseract
from fastapi import FastAPI, File, UploadFile, Request, Form, HTTPException, BackgroundTasks, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image # Pillow
from pydantic import BaseModel # For request body validation
from pypdf import PdfWriter, PdfReader

from celery_worker import celery_app, ocr_single_image_task, process_and_generate_merged_pdf_task

try:
    pytesseract.get_tesseract_version()
    print("Tesseract found in PATH.")
except pytesseract.TesseractNotFoundError:
    tesseract_path_windows = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    tesseract_path_linux = '/usr/bin/tesseract'

    if os.name == 'nt' and Path(tesseract_path_windows).exists():
        pytesseract.pytesseract.tesseract_cmd = tesseract_path_windows
        print(f"Tesseract path set to: {tesseract_path_windows}")
    elif Path(tesseract_path_linux).exists():
        pytesseract.pytesseract.tesseract_cmd = tesseract_path_linux
        print(f"Tesseract path set to: {tesseract_path_linux}")
    else:
        print("ERROR: Tesseract not found automatically or at specified paths.")
        raise RuntimeError("Tesseract OCR engine not found. Please install or configure it.")


UPLOAD_DIR = Path("uploads")
PDF_DIR = Path("generated_pdfs")
STATIC_DIR = Path("static")

UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
Path(STATIC_DIR / "css").mkdir(exist_ok=True, parents=True)
Path(STATIC_DIR / "js").mkdir(exist_ok=True, parents=True)


app = FastAPI()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="templates")


def cleanup_files(files_to_delete: List[Path]):
    print(f"Cleanup task: Attempting to delete {len(files_to_delete)} files.")
    for f_path in files_to_delete:
        try:
            if f_path.is_file():
                os.remove(f_path)
                print(f"Successfully deleted temp file: {f_path}")
            elif f_path.exists():
                 print(f"Path exists but is not a file, skipping deletion: {f_path}")
        except OSError as e:
            print(f"Error deleting temp file {f_path}: {e}")

# Hàm này được dùng bởi Celery worker, nhưng cũng có thể dùng bởi endpoint đồng bộ
# Nếu chỉ Celery dùng, có thể chuyển hoàn toàn vào celery_worker.py
def create_searchable_pdf_page_internal(image_path: Path, lang: str = 'vie') -> bytes:
    """Internal synchronous function to create searchable PDF page."""
    try:
        pdf_bytes = pytesseract.image_to_pdf_or_hocr(str(image_path), lang=lang, extension='pdf')
        print(f"Internal: Successfully created searchable PDF page for: {image_path} with lang: {lang}")
        return pdf_bytes
    except pytesseract.TesseractNotFoundError:
        print("Internal ERROR: Tesseract is not installed or not in your PATH/configured correctly.")
        raise # Re-raise for Celery or calling function to handle
    except Exception as e:
        print(f"Internal Error creating searchable PDF page for {image_path}: {e}")
        raise # Re-raise

async def create_searchable_pdf_page(image_path: Path, lang: str = 'vie') -> bytes:
    """Async wrapper for FastAPI endpoint if needed, or just use internal for sync calls."""
    try:
        return create_searchable_pdf_page_internal(image_path, lang)
    except Exception as e: # Catch re-raised exceptions
        if isinstance(e, pytesseract.TesseractNotFoundError):
             raise HTTPException(status_code=500, detail="Tesseract OCR engine not found or configured incorrectly.")
        raise HTTPException(status_code=500, detail=f"Error during searchable PDF page generation: {e}")


def merge_pdfs(pdf_bytes_list: List[bytes], output_path: Path) -> Path:
    merger = PdfWriter()
    processed_page_count = 0
    try:
        if not pdf_bytes_list:
            raise ValueError("No PDF pages provided to merge.")

        for i, pdf_bytes in enumerate(pdf_bytes_list):
            if not pdf_bytes:
                print(f"Warning: Skipping empty PDF bytes at index {i}.")
                continue
            try:
                pdf_stream = io.BytesIO(pdf_bytes)
                reader = PdfReader(pdf_stream)
                if len(reader.pages) > 0:
                     merger.append(reader)
                     processed_page_count += len(reader.pages)
            except Exception as page_err:
                print(f"Error processing PDF page bytes at index {i}: {page_err}. Skipping this page.")
                continue

        if processed_page_count == 0:
            raise ValueError("Merging resulted in an empty PDF. Check individual page generation.")

        with open(output_path, "wb") as f_out:
            merger.write(f_out)
        merger.close()
        print(f"Successfully merged {processed_page_count} pages into: {output_path}")
        return output_path
    except Exception as e:
        print(f"Error merging PDFs into {output_path}: {e}")
        if output_path.exists():
            try: os.remove(output_path)
            except OSError as rm_err: print(f"Error removing incomplete merged PDF {output_path}: {rm_err}")
        # Không raise HTTPException ở đây nếu hàm này được Celery gọi
        # Celery task sẽ tự xử lý exception. Nếu gọi từ FastAPI endpoint thì mới raise HTTPException
        raise # Re-raise để Celery biết lỗi hoặc endpoint xử lý


# --- Pydantic Models for Request Bodies ---
class OCRPageInfo(BaseModel):
    temp_filename: str
    original_filename: str

class OCRMultiplePagesPayload(BaseModel):
    pages_to_ocr: List[OCRPageInfo]
    lang: str

class FileInfo(BaseModel):
    temp_filename: str
    lang: Optional[str] = 'vie'
    original_filename: Optional[str] = None

class GeneratePdfPayload(BaseModel):
    files_to_process: List[FileInfo]

# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index_alpine.html", {"request": request})

@app.post("/upload/", response_class=JSONResponse)
async def upload_images_for_processing(files: List[UploadFile] = File(...)):
    uploaded_file_info = []
    temp_files_created_this_request = []

    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")
    print(f"Received {len(files)} files for upload.")

    for file in files:
        if not file.content_type or not file.content_type.startswith("image/"):
            print(f"Skipping non-image file: {file.filename} (Type: {file.content_type})")
            continue
        temp_filepath = None
        try:
            file_suffix = Path(file.filename).suffix.lower() if Path(file.filename).suffix else '.png'
            allowed_suffixes = ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif', '.webp']
            if file_suffix not in allowed_suffixes:
                print(f"Skipping file with unsupported suffix: {file.filename}")
                continue

            temp_filename_stem = uuid.uuid4()
            temp_filename = f"{temp_filename_stem}{file_suffix}"
            temp_filepath = UPLOAD_DIR / temp_filename
            
            with open(temp_filepath, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            temp_files_created_this_request.append(temp_filepath)

            uploaded_file_info.append({
                "original_filename": file.filename,
                "temp_filename": temp_filename,
            })
        except Exception as e:
            print(f"Error processing file {file.filename} during upload: {e}")
            if temp_files_created_this_request:
                cleanup_files(temp_files_created_this_request)
            if hasattr(file, 'file') and not file.file.closed:
                 await file.close()
            raise HTTPException(status_code=500, detail=f"Error processing {file.filename}: {e}")
        finally:
             if hasattr(file, 'file') and not file.file.closed:
                await file.close()
    
    if not uploaded_file_info:
        raise HTTPException(status_code=400, detail="No valid image files were processed.")
    print(f"Finished processing upload batch. Successfully saved {len(uploaded_file_info)} files.")
    return JSONResponse(content={"uploaded_files": uploaded_file_info})


@app.post("/ocr-page/", response_class=JSONResponse)
async def ocr_single_page_text_sync(payload: FileInfo = Body(...)):
    temp_filename = payload.temp_filename
    lang = payload.lang
    original_filename = payload.original_filename or temp_filename

    if not temp_filename:
        raise HTTPException(status_code=400, detail="Missing 'temp_filename' in payload.")
    image_path = UPLOAD_DIR / temp_filename
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail=f"Temporary file '{temp_filename}' not found for OCR.")

    try:
        print(f"SYNC OCR on {image_path} with lang: {lang}")
        with Image.open(image_path) as img:
            text_content = pytesseract.image_to_string(img, lang=lang)
        print(f"SYNC OCR successful for {image_path}")
        return JSONResponse(content={
            "text": text_content.strip(),
            "temp_filename": temp_filename,
            "original_filename": original_filename
        })
    except pytesseract.TesseractNotFoundError:
        raise HTTPException(status_code=500, detail="Tesseract OCR engine not found or configured incorrectly.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during SYNC OCR for page '{original_filename}': {e}")


@app.post("/ocr-multiple-pages/", response_class=JSONResponse)
async def ocr_multiple_pages_async(payload: OCRMultiplePagesPayload):
    pages_data = payload.pages_to_ocr
    lang = payload.lang

    if not pages_data:
        raise HTTPException(status_code=400, detail="No pages specified for OCR.")

    submitted_tasks_info = []
    print(f"Received request to OCR {len(pages_data)} pages with language: {lang}")

    for page_info in pages_data:
        temp_filename = page_info.temp_filename
        original_filename = page_info.original_filename
        image_path = UPLOAD_DIR / temp_filename

        if not image_path.is_file():
            print(f"Warning: Image file for OCR not found: {image_path} (original: {original_filename}). Skipping.")
            submitted_tasks_info.append({
                "original_filename": original_filename, "temp_filename": temp_filename,
                "status": "error", "task_id": None, "detail": "File not found on server."
            })
            continue
        try:
            task = ocr_single_image_task.delay(
                image_path_str=str(image_path), lang=lang,
                original_filename=original_filename, temp_filename=temp_filename
            )
            submitted_tasks_info.append({
                "original_filename": original_filename, "temp_filename": temp_filename,
                "task_id": task.id, "status": "submitted"
            })
            print(f"Submitted OCR task {task.id} for {original_filename}")
        except Exception as e:
            print(f"Error submitting OCR task for {original_filename}: {e}")
            submitted_tasks_info.append({
                "original_filename": original_filename, "temp_filename": temp_filename,
                "status": "error", "task_id": None, "detail": f"Failed to submit task to Celery: {e}"
            })
            
    successful_submissions = [t for t in submitted_tasks_info if t["status"] == "submitted"]
    if not successful_submissions:
        # Check if any "file not found" errors occurred before raising 500
        if any(info.get("detail") == "File not found on server." for info in submitted_tasks_info):
             raise HTTPException(status_code=404, detail="One or more image files not found on server for OCR.")
        raise HTTPException(status_code=500, detail="Failed to submit any OCR tasks to Celery.")

    return {
        "message": f"Submitted {len(successful_submissions)} OCR tasks out of {len(pages_data)} requested pages.",
        "submitted_tasks": submitted_tasks_info
    }

@app.get("/task-status/{task_id}", response_class=JSONResponse)
async def get_task_status(task_id: str):
    task_result = celery_app.AsyncResult(task_id)
    response_data = {"task_id": task_id, "status": task_result.status, "result": None}
    if task_result.successful():
        response_data["result"] = task_result.get()
    elif task_result.failed():
        result_error = task_result.result
        if isinstance(result_error, Exception):
            response_data["result"] = {"error": str(result_error), "traceback": task_result.traceback}
        else:
            response_data["result"] = result_error # Should be the dict from task
    elif task_result.status == 'PROGRESS': # If task updates progress
        response_data["result"] = task_result.info
    return response_data


@app.post("/generate-single-page-pdf/", response_class=FileResponse)
async def generate_single_page_searchable_pdf_endpoint( # Renamed to avoid conflict if old one exists
    background_tasks: BackgroundTasks,
    file_info: FileInfo = Body(...)
    ):
    temp_filename = file_info.temp_filename
    lang = file_info.lang
    original_filename = file_info.original_filename or temp_filename
    original_filename_base = Path(original_filename).stem

    if not temp_filename:
        raise HTTPException(status_code=400, detail="Missing 'temp_filename'.")
    image_path = UPLOAD_DIR / temp_filename
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail=f"Temporary file {temp_filename} not found.")

    pdf_temp_path = None
    try:
        pdf_bytes = await create_searchable_pdf_page(image_path, lang=lang) # uses async wrapper
        pdf_filename_base = f"page_{original_filename_base}_{uuid.uuid4()}"
        pdf_temp_path = PDF_DIR / f"{pdf_filename_base}.pdf"
        with open(pdf_temp_path, "wb") as f_out: f_out.write(pdf_bytes)
        
        # Schedule cleanup of the generated PDF itself
        background_tasks.add_task(cleanup_files, [pdf_temp_path])
        print(f"Scheduled cleanup for temporary single PDF: {pdf_temp_path}")
        return FileResponse(path=pdf_temp_path, filename=f"{original_filename_base}_searchable.pdf", media_type='application/pdf')
    except HTTPException as e:
         if pdf_temp_path and pdf_temp_path.exists(): cleanup_files([pdf_temp_path])
         raise e
    except Exception as e:
        print(f"Unexpected error generating single page PDF for {temp_filename}: {e}")
        if pdf_temp_path and pdf_temp_path.exists(): cleanup_files([pdf_temp_path])
        raise HTTPException(status_code=500, detail=f"Failed to create PDF for {original_filename}: {e}")


@app.delete("/delete-temp-file/{temp_filename}")
async def delete_temp_file_endpoint(temp_filename: str, background_tasks: BackgroundTasks):
    if not temp_filename or ".." in temp_filename or temp_filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid temporary filename.")
    file_path = UPLOAD_DIR / temp_filename
    print(f"Received request to delete temp file: {file_path}")
    if not file_path.exists():
         return JSONResponse(content={"message": f"File {temp_filename} not found or already deleted."}, status_code=404)
    if not file_path.is_file():
         raise HTTPException(status_code=400, detail="Target path is not a file.")
    background_tasks.add_task(cleanup_files, [file_path])
    print(f"Scheduled deletion for temp file: {file_path}")
    return JSONResponse(content={"message": f"Deletion scheduled for file {temp_filename}."})

# Endpoint để tải file PDF đã xử lý bởi Celery (ví dụ từ process_book_pages_task)
@app.get("/download-celery-pdf/{filename}", response_class=FileResponse)
async def download_celery_processed_pdf(filename: str, background_tasks: BackgroundTasks):
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    file_path = PDF_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Processed PDF file (from Celery) not found.")
    
    # Quan trọng: Lên lịch xóa file này SAU KHI được gửi đi
    background_tasks.add_task(cleanup_files, [file_path])
    print(f"Scheduled cleanup for Celery-generated PDF: {file_path} after sending.")
    
    return FileResponse(path=file_path, filename=filename, media_type='application/pdf')

@app.post("/generate-book-pdf-async/", response_class=JSONResponse)
async def generate_book_pdf_async_endpoint(payload: GeneratePdfPayload):
    """
    Nhận yêu cầu tạo PDF tổng hợp và gửi một task điều phối cho Celery.
    """
    files_to_process_pydantic = payload.files_to_process
    if not files_to_process_pydantic:
        raise HTTPException(status_code=400, detail="No files specified for PDF generation.")

    # **THAY ĐỔI QUAN TRỌNG: Chuyển đổi Pydantic model sang dict thuần túy**
    files_to_process_plain = [item.dict() for item in files_to_process_pydantic]
    
    print(f"\n[ENDPOINT /generate-book-pdf-async/] Received request to generate PDF for {len(files_to_process_plain)} pages.")
    print(f"[ENDPOINT /generate-book-pdf-async/] Payload to send to Celery: {files_to_process_plain}")


    try:
        # Gọi task chính với dữ liệu đã được làm sạch
        task = process_and_generate_merged_pdf_task.delay(files_to_process_plain) # Sử dụng biến mới
        
        print(f"[ENDPOINT /generate-book-pdf-async/] Submitted main PDF generation task with ID: {task.id}")
        return {
            "message": "PDF generation task submitted successfully.",
            "task_id": task.id,
            "status_check_url": f"/task-status/{task.id}"
        }
    except Exception as e:
        print(f"[ENDPOINT /generate-book-pdf-async/] CRITICAL ERROR submitting task: {e}")
        # In ra traceback đầy đủ để gỡ lỗi
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Could not submit PDF generation task to the queue. Check server logs for details.")
    
@app.get("/download-generated-pdf/{filename}", response_class=FileResponse)
async def download_generated_pdf(filename: str, background_tasks: BackgroundTasks):
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    file_path = PDF_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Generated PDF file not found.")
    
    # Quan trọng: Lên lịch xóa file này SAU KHI được gửi đi
    background_tasks.add_task(os.remove, file_path)
    print(f"Serving and scheduling cleanup for: {file_path}")
    
    return FileResponse(path=file_path, filename=filename, media_type='application/pdf')

if __name__ == "__main__":
    import uvicorn
    Path("templates").mkdir(exist_ok=True)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)