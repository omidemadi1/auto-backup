import os
import json
import hashlib
import tarfile
import zipfile
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateForumTopicRequest
from dotenv import load_dotenv

# بارگذاری تنظیمات از فایل .env
load_dotenv()

# --- پیکربندی (Configuration) ---
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROUP_CHAT_ID = int(os.getenv('GROUP_CHAT_ID'))

STATE_FILE = os.getenv('STATE_FILE')
PROJECTS_DIR = os.getenv('PROJECTS_DIR')
NEXTCLOUD_DIR = os.getenv('NEXTCLOUD_DIR')

MAX_FILE_SIZE_GB = float(os.getenv('MAX_FILE_SIZE_GB', 1.9))
MAX_FILE_SIZE = int(MAX_FILE_SIZE_GB * 1024 * 1024 * 1024) # تبدیل به بایت
RETENTION_LIMIT = int(os.getenv('RETENTION_LIMIT', 10))

client = TelegramClient('backup_session', API_ID, API_HASH)

# --- مدیریت وضعیت (State Management) ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"projects": {}, "nextcloud_photos": {"topic_id": None, "last_checked_timestamp": 0}}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

# --- توابع کمکی (Helpers) ---
def get_directory_hash(directory):
    """تولید هش از محتویات دایرکتوری برای تشخیص تغییرات"""
    sha256_hash = hashlib.sha256()
    for root, _, files in os.walk(directory):
        for names in files:
            filepath = os.path.join(root, names)
            try:
                with open(filepath, "rb") as f:
                    for byte_block in iter(lambda: f.read(4096), b""):
                        sha256_hash.update(byte_block)
            except OSError:
                continue
    return sha256_hash.hexdigest()

def split_file(filepath):
    """تکه‌تکه کردن فایل‌های بزرگ"""
    file_size = os.path.getsize(filepath)
    if file_size <= MAX_FILE_SIZE:
        return [filepath]
    
    parts = []
    part_num = 1
    with open(filepath, 'rb') as f:
        while chunk := f.read(MAX_FILE_SIZE):
            part_name = f"{filepath}.part{part_num}"
            with open(part_name, 'wb') as p:
                p.write(chunk)
            parts.append(part_name)
            part_num += 1
    return parts

async def get_or_create_topic(topic_name, state_topic_id):
    """بررسی یا ایجاد تاپیک جدید در گروه تلگرام"""
    if state_topic_id:
        return state_topic_id
    result = await client(CreateForumTopicRequest(channel=GROUP_CHAT_ID, title=topic_name))
    return result.updates[0].id

async def upload_files(topic_id, file_paths):
    """آپلود فایل‌ها به تاپیک مشخص شده"""
    msg_ids = []
    for path in file_paths:
        msg = await client.send_file(GROUP_CHAT_ID, path, reply_to=topic_id)
        msg_ids.append(msg.id)
    return msg_ids

# --- پایپ‌لاین A: پروژه‌ها ---
async def backup_projects(state):
    if not os.path.exists(PROJECTS_DIR):
        print(f"Projects directory not found: {PROJECTS_DIR}")
        return
    
    for project_name in os.listdir(PROJECTS_DIR):
        project_path = os.path.join(PROJECTS_DIR, project_name)
        if not os.path.isdir(project_path):
            continue
        
        current_hash = get_directory_hash(project_path)
        proj_state = state["projects"].get(project_name, {"topic_id": None, "last_hash": "", "backups": []})
        
        if current_hash == proj_state.get("last_hash"):
            print(f"Skipping {project_name}: No changes detected.")
            continue
        
        print(f"Backing up {project_name}...")
        archive_name = f"/tmp/{project_name}_{datetime.now().strftime('%Y%m%d')}.tar.gz"
        
        # 1. ساخت آرشیو
        with tarfile.open(archive_name, "w:gz") as tar:
            tar.add(project_path, arcname=os.path.basename(project_path))
        
        # 2. بررسی سایز و تکه‌تکه کردن
        files_to_upload = split_file(archive_name)
        
        # 3. مدیریت تاپیک و آپلود
        topic_id = await get_or_create_topic(f"Backup: {project_name}", proj_state.get("topic_id"))
        proj_state["topic_id"] = topic_id
        uploaded_msg_ids = await upload_files(topic_id, files_to_upload)
        
        # 4. آپدیت وضعیت و اعمال قانون نگهداری (Retention Rule)
        date_str = datetime.now().strftime("%Y-%m-%d")
        proj_state["backups"].append({"date": date_str, "message_ids": uploaded_msg_ids})
        proj_state["last_hash"] = current_hash
        
        # پاک کردن فایل‌های موقت
        for f in files_to_upload:
            if os.path.exists(f): os.remove(f)
        if os.path.exists(archive_name): os.remove(archive_name)
        
        # حذف بکاپ‌های قدیمی از تلگرام
        if len(proj_state["backups"]) > RETENTION_LIMIT:
            oldest_backup = proj_state["backups"].pop(0)
            await client.delete_messages(GROUP_CHAT_ID, oldest_backup["message_ids"])
            print(f"Deleted old backup from telegram for {project_name}.")
            
        state["projects"][project_name] = proj_state

# --- پایپ‌لاین B: عکس‌های نکست‌کلود ---
async def backup_nextcloud_photos(state):
    if not os.path.exists(NEXTCLOUD_DIR):
        print(f"Nextcloud directory not found: {NEXTCLOUD_DIR}")
        return
    
    nc_state = state.get("nextcloud_photos", {"topic_id": None, "last_checked_timestamp": 0})
    last_timestamp = nc_state["last_checked_timestamp"]
    new_files = []
    
    # 1. پیدا کردن فایل‌های جدید
    for root, _, files in os.walk(NEXTCLOUD_DIR):
        for name in files:
            filepath = os.path.join(root, name)
            mtime = os.path.getmtime(filepath)
            if mtime > last_timestamp:
                new_files.append(filepath)
                
    if not new_files:
        print("No new photos found in Nextcloud.")
        return
        
    print(f"Backing up {len(new_files)} new Nextcloud photos...")
    
    # 2. ساخت فایل زیپ از عکس‌های جدید
    current_time = datetime.now().timestamp()
    archive_name = f"/tmp/Photos_{datetime.now().strftime('%Y%m%d')}.zip"
    
    with zipfile.ZipFile(archive_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in new_files:
            zipf.write(file, os.path.relpath(file, NEXTCLOUD_DIR))
            
    # 3. بررسی سایز و آپلود
    files_to_upload = split_file(archive_name)
    topic_id = await get_or_create_topic("Nextcloud Photos", nc_state.get("topic_id"))
    nc_state["topic_id"] = topic_id
    
    await upload_files(topic_id, files_to_upload)
    
    # 4. آپدیت وضعیت
    nc_state["last_checked_timestamp"] = current_time
    state["nextcloud_photos"] = nc_state
    
    # پاک کردن فایل‌های موقت
    for f in files_to_upload:
        if os.path.exists(f): os.remove(f)
    if os.path.exists(archive_name): os.remove(archive_name)

# --- اجرای اصلی ---
async def main():
    await client.start(bot_token=BOT_TOKEN)
    state = load_state()
    
    print("Starting Project Backups...")
    await backup_projects(state)
    
    print("Starting Nextcloud Photos Backup...")
    await backup_nextcloud_photos(state)
    
    save_state(state)
    print("Backup pipeline finished successfully.")

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
