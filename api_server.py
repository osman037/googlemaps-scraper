from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import subprocess
import os
import uuid
import csv
import io

app = FastAPI(title="Google Maps Scraper API")

# In-memory job store (resets on container restart)
jobs = {}

class ScrapeRequest(BaseModel):
    query: str
    max_pages: int = 1          # 20 results per page
    workers: int = 1
    no_emails: bool = False
    no_details: bool = False

def run_scraper(job_id: str, request: ScrapeRequest):
    """Runs the scraper CLI in the background."""
    output_dir = f"/tmp/scrape_{job_id}"
    os.makedirs(output_dir, exist_ok=True)
    out_csv = os.path.join(output_dir, "businesses.csv")

    command = [
        "python", "main.py", "scrape",
        "--query", request.query,
        "--max-pages", str(request.max_pages),
        "--workers", str(request.workers),
        "--out", out_csv
    ]
    if request.no_emails:
        command.append("--no-emails")
    if request.no_details:
        command.append("--no-details")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,  # 15 minutes
            cwd="/app"
        )

        if result.returncode != 0:
            jobs[job_id] = {"status": "failed", "error": result.stderr[-1000:]}
            return

        # Read CSV and convert to JSON
        places = []
        if os.path.exists(out_csv):
            with open(out_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                places = list(reader)

        jobs[job_id] = {
            "status": "completed",
            "logs": result.stdout[-500:],
            "count": len(places),
            "data": places
        }

    except subprocess.TimeoutExpired:
        jobs[job_id] = {"status": "failed", "error": "Scraper timed out."}
    except Exception as e:
        jobs[job_id] = {"status": "failed", "error": str(e)}

@app.get("/")
def root():
    return {"status": "running", "message": "Google Maps Scraper API is live."}

@app.post("/scrape")
def start_scrape(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """Returns immediately with a job ID."""
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running"}

    background_tasks.add_task(run_scraper, job_id, request)

    return {
        "status": "accepted",
        "job_id": job_id,
        "check_status_url": f"/status/{job_id}"
    }

@app.get("/status/{job_id}")
def get_status(job_id: str):
    """Poll this endpoint until status is 'completed' or 'failed'."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    return jobs[job_id]
