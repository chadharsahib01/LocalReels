import os
import shutil
import asyncio
import json
import uuid
import httpx
from datetime import datetime
from .logger import logger
from .database import SessionLocal
from .models import Video
from .utils import has_enough_space, get_file_hash_async # Hashing import add kar diya

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp")
MEDIA_DIR = r"C:\LocalReels\media"
os.makedirs(TMP_DIR, exist_ok=True)

def check_ffmpeg(): return shutil.which("ffmpeg") is not None

def cleanup_temp_files():
    for f in os.listdir(TMP_DIR):
        file_path = os.path.join(TMP_DIR, f)
        try:
            if os.path.isfile(file_path): os.unlink(file_path)
            elif os.path.isdir(file_path): shutil.rmtree(file_path)
        except Exception as e:
            logger.error(f"Cleanup Error: {e}")

async def fallback_redgifs_download(url: str, output_path: str):
    logger.info("RedGifs Native API Fallback try kar rahe hain...")
    if "redgifs.com" not in url: return False
    try:
        async with httpx.AsyncClient() as client:
            auth_res = await client.get("https://api.redgifs.com/v2/auth/temporary")
            token = auth_res.json().get("token")
            if not token: return False
            
            video_id = url.rstrip('/').split('/')[-1]
            headers = {"Authorization": f"Bearer {token}"}
            info_res = await client.get(f"https://api.redgifs.com/v2/gifs/{video_id}", headers=headers)
            
            video_data = info_res.json().get("gif", {})
            dl_link = video_data.get("urls", {}).get("hd")
            
            if dl_link:
                video_res = await client.get(dl_link, headers=headers)
                with open(output_path, 'wb') as f: f.write(video_res.content)
                logger.info("RedGifs Native API se video download ho gayi!")
                return True
    except Exception as e:
        logger.error(f"RedGifs API fail: {e}")
    return False

async def fallback_cobalt_download(url: str, output_path: str):
    logger.info("Cobalt API fallback active...")
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("https://co.wuk.sh/api/json", json={"url": url, "vCodec": "h264"}, headers={"Accept": "application/json"})
            if res.status_code == 200 and "url" in res.json():
                video_res = await client.get(res.json()["url"])
                with open(output_path, 'wb') as f: f.write(video_res.content)
                return True
    except Exception as e:
        logger.error(f"Cobalt bhi fail: {e}")
    return False

async def process_video_ffmpeg(input_file: str, output_file: str):
    if not check_ffmpeg(): return
    logger.info("Normalizing Audio...")
    cmd = ["ffmpeg", "-y", "-i", input_file, "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", output_file]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    if os.path.exists(output_file): os.replace(output_file, input_file)

async def background_download_task(video_url: str):
    if not has_enough_space(MEDIA_DIR): return False

    video_id = uuid.uuid4().hex
    mp4_path = os.path.join(MEDIA_DIR, f"{video_id}.mp4")
    temp_norm_path = os.path.join(TMP_DIR, f"{video_id}_norm.mp4")
    metadata = {"title": f"Video {video_id[:6]}", "duration": 0, "width": 1280, "height": 720, "hash": ""}

    try:
        info_cmd = ["yt-dlp", "-J", "--no-playlist", video_url]
        proc = await asyncio.create_subprocess_exec(*info_cmd, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            info = json.loads(stdout.decode())
            metadata["title"] = info.get("title", metadata["title"])
            metadata["duration"] = info.get("duration", 0)
            metadata["width"] = info.get("width", 1280)
            metadata["height"] = info.get("height", 720)

        cmd = ["yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best", "-o", mp4_path, "--write-thumbnail", video_url]
        dl_proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        await dl_proc.communicate()
        if dl_proc.returncode != 0: raise Exception("yt-dlp failed")

    except Exception as e:
        logger.warning(f"yt-dlp fail, fallbacks trying. Error: {e}")
        success = await fallback_redgifs_download(video_url, mp4_path)
        if not success: success = await fallback_cobalt_download(video_url, mp4_path)
        if not success: return False
            
    if os.path.exists(mp4_path): await process_video_ffmpeg(mp4_path, temp_norm_path)

    # 🚨 NAYA: Duplicate File Hash Checking (No App Hangs)
    if os.path.exists(mp4_path):
        file_hash = await get_file_hash_async(mp4_path)
        db = SessionLocal()
        try:
            duplicate = db.query(Video).filter(Video.payload.ilike(f'%"{file_hash}"%')).first()
            if duplicate:
                logger.warning(f"Duplicate mili! Ye video pehle se DB mein hai.")
                os.remove(mp4_path)
                jpg_path = mp4_path.replace('.mp4', '.jpg')
                if os.path.exists(jpg_path): os.remove(jpg_path)
                return False
            metadata["hash"] = file_hash
        finally:
            db.close()

    db = SessionLocal()
    try:
        max_ordinal = db.query(Video).count() + 1
        new_video_data = {
            "id": video_id, "url": video_url, "title": metadata["title"],
            "added": datetime.now().isoformat(), "group": "Downloads", "status": "ready",
            "duration": metadata["duration"], "width": metadata["width"], "height": metadata["height"],
            "hash": metadata["hash"] # Hash saved
        }
        new_video = Video(id=video_id, payload=json.dumps(new_video_data), status="ready", ordinal=max_ordinal)
        db.add(new_video)
        db.commit()
    finally:
        db.close()
    return True