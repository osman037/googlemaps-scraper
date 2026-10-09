FROM python:3.11-slim

WORKDIR /app

# Install system deps (if needed for curl_cffi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install FastAPI + Uvicorn
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copy the rest of the app
COPY . .

# Expose the port Northflank will map
EXPOSE 8000

# Start the API server
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
