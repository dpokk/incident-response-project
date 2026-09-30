FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Fixed mmap threshold so freed per-request buffers are returned to the OS
    # (otherwise glibc's dynamic threshold makes RSS "sticky" after a spike).
    MALLOC_MMAP_THRESHOLD_=131072

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

USER 10001
# Entrypoint is chosen per Deployment: backend.py, frontend.py or loadgen.py
CMD ["python", "backend.py"]
