import os
import shutil
import datetime
from .logger import logger

def has_enough_space(path: str, required_bytes: int = 100 * 1024 * 1024) -> bool:
    try:
        total, used, free = shutil.disk_usage(path)
        if free < required_bytes:
            logger.warning(f"Disk space low in {path}! Free: {free} bytes.")
            return False
        return True
    except Exception as e:
        logger.error(f"Disk space check failed: {e}")
        return False

def backup_database(db_path: str):
    backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    backup_file = os.path.join(backup_dir, f"db_backup_{today}.sqlite3")
    
    if not os.path.exists(backup_file):
        try:
            shutil.copy2(db_path, backup_file)
            logger.info(f"Database ka naya backup ban gaya: {backup_file}")
            
            for f in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, f)
                if os.stat(file_path).st_mtime < (datetime.datetime.now() - datetime.timedelta(days=30)).timestamp():
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"Backup fail ho gaya: {e}")




import hashlib
import asyncio

# Fix #16: Partial Download Cleanup (.part, .ytdl files jo download fail hone par reh jati hain)
def cleanup_partial_downloads(media_path: str):
    try:
        for f in os.listdir(media_path):
            if f.endswith(('.part', '.ytdl', '.temp')):
                file_path = os.path.join(media_path, f)
                os.remove(file_path)
                logger.info(f"Kachhi (Orphan) file delete ki: {f}")
    except Exception as e:
        logger.error(f"Partial cleanup error: {e}")

# Fix #20: Async Hashing (Duplicate check karne ke liye, bina app ko hang kiye)
async def get_file_hash_async(file_path: str) -> str:
    # Badi files ka hash background thread mein calculate karna taake UI na ruke
    def calculate():
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest()
    
    return await asyncio.to_thread(calculate)