import json
import os
import html
from fastapi import FastAPI, Depends, Request, BackgroundTasks, Body, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from . import models
from .database import SessionLocal
from .config import settings
from .logger import logger
from .downloader import cleanup_temp_files, background_download_task
from .utils import backup_database, cleanup_partial_downloads

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title=settings.APP_NAME)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, 
    allow_methods=["*"], allow_headers=["*"]
)

MEDIA_PATH = r"C:\LocalReels\media"
FRONTEND_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

app.mount("/media", StaticFiles(directory=MEDIA_PATH), name="media")
app.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")

@app.on_event("startup")
def startup_event():
    logger.info("Server start ho raha hai...")
    cleanup_temp_files()
    cleanup_partial_downloads(MEDIA_PATH)
    backup_database(r"C:\LocalReels\localreels_v8.sqlite3")

def get_db():
    db = SessionLocal()
    try: 
        yield db
    finally: 
        db.close()

@app.get("/")
def read_root():
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))

@app.get("/api/videos")
@limiter.limit("30/minute")
def get_videos(request: Request, skip: int = 0, limit: int = 12, search: str = "", sort_by: str = "newest", db: Session = Depends(get_db)):
    try:
        query = db.query(models.Video)
        if search:
            query = query.filter(models.Video.payload.ilike(f"%{search}%"))
            
        if sort_by == "oldest":
            query = query.order_by(models.Video.ordinal)
        else:
            query = query.order_by(desc(models.Video.ordinal))
            
        videos = query.offset(skip).limit(limit).all()
        
        result = []
        for v in videos:
            if v.payload:
                data = json.loads(v.payload)
                data["title"] = html.escape(data.get("title", ""))
                data["group"] = html.escape(data.get("group", "Local"))
                result.append(data)
                
        return {"status": "Success", "fetched": len(result), "videos": result}
    except Exception as e:
        logger.error(f"Error fetching videos: {str(e)}", exc_info=True)
        return {"status": "Error", "message": "Logs check karein."}

@app.post("/api/download")
def start_download(background_tasks: BackgroundTasks, url: str = Body(..., embed=True), x_localreels_auth: str = Header(None)):
    if x_localreels_auth != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized request")
    background_tasks.add_task(background_download_task, url)
    return {"status": "Success", "message": "Download process started!"}

@app.post("/api/videos/bulk-delete")
def bulk_delete_videos(video_ids: list[str] = Body(...), db: Session = Depends(get_db)):
    try:
        db.query(models.Video).filter(models.Video.id.in_(video_ids)).delete(synchronize_session=False)
        db.commit()
        for vid in video_ids:
            mp4_path = os.path.join(MEDIA_PATH, f"{vid}.mp4")
            jpg_path = os.path.join(MEDIA_PATH, f"{vid}.jpg")
            if os.path.exists(mp4_path): os.remove(mp4_path)
            if os.path.exists(jpg_path): os.remove(jpg_path)
        return {"status": "Success", "deleted_count": len(video_ids)}
    except Exception as e:
        return {"status": "Error"}

@app.post("/api/videos/update-group")
def update_video_group(video_ids: list[str] = Body(...), new_group: str = Body(...), db: Session = Depends(get_db)):
    try:
        videos = db.query(models.Video).filter(models.Video.id.in_(video_ids)).all()
        for v in videos:
            if v.payload:
                data = json.loads(v.payload)
                data["group"] = new_group
                v.payload = json.dumps(data)
        db.commit()
        return {"status": "Success", "message": f"{len(video_ids)} videos moved"}
    except Exception as e:
        return {"status": "Error", "message": str(e)}

@app.post("/api/videos/update-tags")
def update_video_tags(video_ids: list[str] = Body(...), tags: list[str] = Body(...), db: Session = Depends(get_db)):
    try:
        videos = db.query(models.Video).filter(models.Video.id.in_(video_ids)).all()
        for v in videos:
            if v.payload:
                data = json.loads(v.payload)
                data["tags"] = tags
                v.payload = json.dumps(data)
        db.commit()
        return {"status": "Success", "message": "Tags updated"}
    except Exception as e:
        return {"status": "Error", "message": str(e)}

@app.post("/api/videos/update-title")
def update_video_title(video_id: str = Body(..., embed=True), new_title: str = Body(..., embed=True), db: Session = Depends(get_db)):
    try:
        video = db.query(models.Video).filter(models.Video.id == video_id).first()
        if video and video.payload:
            data = json.loads(video.payload)
            data["title"] = new_title
            video.payload = json.dumps(data)
            db.commit()
            return {"status": "Success"}
        return {"status": "Error", "message": "Not found"}
    except Exception as e:
        return {"status": "Error", "message": str(e)}

@app.post("/api/settings/update")
def update_settings(max_jobs: int = Body(..., embed=True), theme: str = Body(..., embed=True)):
    settings.MAX_PARALLEL_JOBS = max_jobs
    return {"status": "Success"}