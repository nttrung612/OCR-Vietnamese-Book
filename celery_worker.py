import os
import shutil
from pathlib import Path
import uuid
import io
from typing import Optional, List

from celery import Celery
from celery.exceptions import Ignore
from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract
from pypdf import PdfWriter, PdfReader

try:
    pytesseract.get_tesseract_version()
    print("Celery Worker: Tesseract found.")
except pytesseract.TesseractNotFoundError:
    tesseract_path_linux = '/usr/bin/tesseract'
    if Path(tesseract_path_linux).exists():
        pytesseract.pytesseract.tesseract_cmd = tesseract_path_linux
        print(f"Celery Worker: Tesseract path set to: {tesseract_path_linux}")
    else:
        # Thử tìm trong PATH nếu đường dẫn cụ thể không có
        if shutil.which("tesseract"):
            pytesseract.pytesseract.tesseract_cmd = "tesseract"
            print("Celery Worker: Tesseract found in system PATH.")
        else:
            print("Celery Worker CRITICAL ERROR: Tesseract not found.")

# --- DIRECTORY SETUP ---

UPLOAD_DIR = Path("uploads")
PDF_DIR = Path("generated_pdfs")
PDF_SINGLE_PAGES_DIR = PDF_DIR / "single_pages"

UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)
PDF_SINGLE_PAGES_DIR.mkdir(exist_ok=True)

# --- CELERY CONFIG ---
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

celery_app = Celery(
    'ocr_tasks',
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=['celery_worker'] # Để Celery tự động tìm các task trong file này
)

# Cấu hình quan trọng cho sự ổn định và hiệu suất
celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='Asia/Ho_Chi_Minh',
    enable_utc=True,
    task_acks_late=True,          # Xác nhận task sau khi hoàn thành (an toàn hơn)
    worker_prefetch_multiplier=1,  # Mỗi worker process chỉ lấy 1 task tại một thời điểm (tốt cho task dài)
    
    # Giới hạn tài nguyên để tránh treo và rò rỉ bộ nhớ
    worker_max_tasks_per_child=10, # Khởi động lại worker con sau 10 task để giải phóng bộ nhớ
    task_soft_time_limit=300,      # Báo lỗi SoftTimeLimitExceeded sau 5 phút
    task_time_limit=360,           # kill task bằng HardTimeLimitExceeded sau 6 phút
)

# --- HELPER FUNCTIONS ---
def cleanup_files_celery(files_to_delete: List[str]):
    """Dọn dẹp danh sách các file dựa trên đường dẫn string."""
    print(f"Celery Cleanup: Attempting to delete {len(files_to_delete)} files.")
    for f_path_str in files_to_delete:
        f_path = Path(f_path_str)
        try:
            if f_path.is_file():
                os.remove(f_path)
        except OSError as e:
            print(f"Celery Cleanup Error deleting {f_path}: {e}")

def create_searchable_pdf_page_for_celery(image_path_str: str, lang: str = 'vie') -> bytes:
    """Tạo nội dung bytes cho một trang PDF tìm kiếm được."""
    image_path = Path(image_path_str)
    try:
        pdf_bytes = pytesseract.image_to_pdf_or_hocr(str(image_path), lang=lang, extension='pdf')
        print(f"Celery: Created searchable PDF page for: {image_path}")
        return pdf_bytes
    except Exception as e:
        print(f"Celery Error (create_searchable_pdf_page_for_celery) for {image_path}: {e}")
        raise # Ném lại lỗi để task cha bắt được


# --- CELERY TASKS ---

@celery_app.task(bind=True, name='ocr_tasks.ocr_single_image', acks_late=True)
def ocr_single_image_task(self, image_path_str: str, lang: str, original_filename: str = None, temp_filename: str = None):
    """
    Task OCR ổn định cho một ảnh duy nhất.
    """
    task_id = self.request.id
    print(f"\n[TASK ID: {task_id}] Starting OCR for: {original_filename or temp_filename}")
    image_path = Path(image_path_str)

    if not image_path.is_file():
        error_msg = f"Image file not found: {image_path_str}"
        print(f"[TASK ID: {task_id}] ERROR: {error_msg}")
        return {"status": "failure", "error": error_msg, "temp_filename": temp_filename}
    try:
        with Image.open(image_path) as img:
            img.verify() # Kiểm tra tính hợp lệ của ảnh
            img_reopened = Image.open(image_path)
            
            # Giới hạn kích thước ảnh để tránh lỗi hết bộ nhớ
            MAX_PIXELS = 100_000_000 # 100 Megapixels
            if img_reopened.width * img_reopened.height > MAX_PIXELS:
                raise ValueError(f"Image too large ({img_reopened.width}x{img_reopened.height}).")
            
            text_content = pytesseract.image_to_string(img_reopened, lang=lang)

        print(f"[TASK ID: {task_id}] SUCCESS for: {original_filename or temp_filename}")
        return {"status": "success", "text": text_content.strip(), "temp_filename": temp_filename, "lang_used": lang}
    except UnidentifiedImageError:
        error_msg = "Cannot identify image file. It may be corrupted."
    except ValueError as ve:
        error_msg = str(ve)
    except Exception as e:
        error_msg = f"An unexpected error occurred during OCR: {e}"
    
    print(f"[TASK ID: {task_id}] ERROR: {error_msg}")
    return {"status": "failure", "error": error_msg, "temp_filename": temp_filename}


@celery_app.task(name='ocr_tasks.create_single_pdf_page')
def create_single_pdf_page_task(image_path_str: str, lang: str) -> Optional[str]:
    """
    [TASK CON] Tạo một trang PDF, lưu vào file tạm và trả về ĐƯỜNG DẪN.
    """
    print(f"[PDF Page Task] Processing: {Path(image_path_str).name}")
    try:
        pdf_bytes = create_searchable_pdf_page_for_celery(image_path_str, lang)
        if not pdf_bytes:
            return None
        
        temp_pdf_path = PDF_SINGLE_PAGES_DIR / f"{uuid.uuid4()}.pdf"
        with open(temp_pdf_path, "wb") as f:
            f.write(pdf_bytes)
        
        return str(temp_pdf_path)
    except Exception as e:
        print(f"[PDF Page Task] ERROR creating PDF page for {Path(image_path_str).name}: {e}")
        return None


@celery_app.task(bind=True, name='ocr_tasks.merge_pdf_pages')
def merge_pdf_pages_task(self, pdf_page_paths: list, task_id_for_filename: str, source_image_paths_to_cleanup: list[str]):
    """
    [TASK CALLBACK] Nhận danh sách đường dẫn các trang PDF, ghép chúng lại.
    """
    print(f"[Merge Task] Received {len(pdf_page_paths)} page paths for main task {task_id_for_filename}")
    
    valid_pdf_page_paths = [path for path in pdf_page_paths if path is not None]
    print(f"[Merge Task] Found {len(valid_pdf_page_paths)} valid PDF pages to merge.")

    if not valid_pdf_page_paths:
        error_msg = "All sub-tasks for PDF page generation failed."
        print(f"[Merge Task] {error_msg}")
        raise Ignore() # Bỏ qua task này, không báo lỗi, không làm gì cả.

    final_pdf_filename = f"merged_book_{task_id_for_filename}.pdf"
    final_pdf_path = PDF_DIR / final_pdf_filename
    merger = PdfWriter()

    try:
        for pdf_path_str in valid_pdf_page_paths:
            try:
                merger.append(pdf_path_str)
            except Exception as page_err:
                print(f"[Merge Task] Error appending page {pdf_path_str}: {page_err}. Skipping.")
        
        if len(merger.pages) == 0:
            raise ValueError("Merging resulted in an empty PDF despite valid paths.")

        with open(final_pdf_path, "wb") as f_out:
            merger.write(f_out)
        merger.close()
        
        print(f"[Merge Task] Merged PDF created at: {final_pdf_path}")

        # Dọn dẹp
        cleanup_files_celery(source_image_paths_to_cleanup)
        cleanup_files_celery(valid_pdf_page_paths)
        
        return {
            "status": "success",
            "message": f"Merged PDF with {len(merger.pages)} pages generated successfully.",
            "merged_pdf_filename": final_pdf_filename,
        }
    except Exception as e:
        error_msg = f"Failed to merge final PDF: {e}"
        print(f"[Merge Task] {error_msg}")
        # Dọn dẹp tất cả file tạm nếu merge lỗi
        cleanup_files_celery(source_image_paths_to_cleanup)
        cleanup_files_celery(valid_pdf_page_paths)
        if final_pdf_path.exists():
            os.remove(final_pdf_path)
        raise # Ném lỗi để task chính (workflow) biết là đã thất bại


@celery_app.task(bind=True, name='ocr_tasks.process_and_generate_merged_pdf')
def process_and_generate_merged_pdf_task(self, files_to_process: list[dict]):
    """
    [TASK CHÍNH] Điều phối quá trình tạo PDF tổng hợp.
    """
    from celery import group, chain

    task_id = self.request.id
    print(f"\n[Main PDF Task ID: {task_id}] Starting workflow for {len(files_to_process)} pages.")

    page_creation_tasks = []
    source_image_paths_for_cleanup = []

    for file_info in files_to_process:
        temp_filename = file_info.get("temp_filename")
        if not temp_filename: continue
        image_path = UPLOAD_DIR / temp_filename
        if not image_path.is_file(): continue

        lang = file_info.get("lang", "vie")
        page_creation_tasks.append(create_single_pdf_page_task.s(str(image_path), lang))
        source_image_paths_for_cleanup.append(str(image_path))

    if not page_creation_tasks:
        raise ValueError("No valid image files found to process.")

    # Workflow: Chạy group song song, sau đó chạy callback để merge
    callback = merge_pdf_pages_task.s(
        task_id_for_filename=task_id, 
        source_image_paths_to_cleanup=source_image_paths_for_cleanup
    )
    workflow = chain(group(page_creation_tasks), callback)
    
    # Thay thế task hiện tại bằng workflow.
    # Celery sẽ theo dõi workflow này bằng task_id của task hiện tại.
    print(f"[Main PDF Task ID: {task_id}] Created workflow. Replacing current task...")
    raise self.replace(workflow)