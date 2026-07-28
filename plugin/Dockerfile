FROM python:3.12-slim

WORKDIR /app

# Install system deps (libGL for Pillow, no OpenCV needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app code + model files
COPY app.py /app/
COPY models/ /app/models/

ENTRYPOINT ["python3", "-u", "/app/app.py"]
