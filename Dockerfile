# Dockerfile

# --- Stage 1: Build Stage ---
# Sử dụng base image đầy đủ hơn để build các dependencies có thể cần C-compiler
FROM python:3.12 as builder

# Đặt biến môi trường
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Cài đặt các thư viện hệ thống cần thiết, bao gồm Tesseract và các gói ngôn ngữ
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-vie \
    tesseract-ocr-eng \
    libgl1-mesa-glx \
    # Thêm các build-essential nếu có gói Python cần biên dịch
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Tạo virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy và cài đặt các thư viện Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# --- Stage 2: Final Stage ---
# Sử dụng base image slim để image cuối cùng nhỏ gọn
FROM python:3.12-slim

# Đặt biến môi trường
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PATH="/opt/venv/bin:$PATH"

# Cài đặt các runtime dependencies (những gì thực sự cần để chạy app)
# Không cần build-essential ở đây
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-vie \
    tesseract-ocr-eng \
    libgl1-mesa-glx \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Tạo một user không phải root để chạy ứng dụng (bảo mật hơn)
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

# Copy virtual environment đã được cài đặt từ build stage
COPY --from=builder /opt/venv /opt/venv

# Tạo các thư mục cần thiết cho ứng dụng
WORKDIR /app
RUN mkdir -p uploads generated_pdfs/single_pages
RUN chown -R appuser:appuser /app

# Copy toàn bộ code ứng dụng
COPY . .

# Đảm bảo toàn bộ thư mục app thuộc sở hữu của user mới
RUN chown -R appuser:appuser /app

# Chuyển sang user không phải root
USER appuser

# Expose port mà FastAPI sẽ chạy
EXPOSE 8000

# Lệnh mặc định để chạy FastAPI server
# CMD sẽ được ghi đè bởi docker-compose.yml, nhưng để đây để image có thể tự chạy
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]