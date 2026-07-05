import os
import re
import sys
import json
import shutil
import logging
import tarfile
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Optional
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import CreateForumTopicRequest
from telethon.tl.types import UpdateNewChannelMessage
from dotenv import load_dotenv
from tqdm import tqdm


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str] = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ── Configuration ────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    required = [
        "API_ID", "API_HASH", "BOT_TOKEN",
        "GROUP_CHAT_ID", "STATE_FILE",
        "PROJECTS_DIR", "ADMIN_USER_ID",
    ]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"FATAL: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    return {
        "api_id": int(os.environ["API_ID"]),
        "api_hash": os.environ["API_HASH"],
        "bot_token": os.environ["BOT_TOKEN"],
        "admin_user_id": int(os.environ["ADMIN_USER_ID"]),
        "group_chat_id": int(os.environ["GROUP_CHAT_ID"]),
        "state_file": os.environ["STATE_FILE"],
        "projects_dir": Path(os.environ["PROJECTS_DIR"]),
        "session_file": os.getenv("SESSION_FILE", "backup_session"),
        "log_file": os.getenv("LOG_FILE"),
        "max_file_size": int(float(os.getenv("MAX_FILE_SIZE_GB", "1.9")) * 1024 ** 3),
        "retention_limit": int(os.getenv("RETENTION_LIMIT", "10")),
    }


# ── State Management ─────────────────────────────────────────────────

STATE_DEFAULTS = {
    "projects": {},
}


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
        state.setdefault("projects", {})
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(STATE_DEFAULTS)


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    Path(tmp).parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
    os.replace(tmp, path)


# ── Helpers ──────────────────────────────────────────────────────────

def get_directory_snapshot(directory: Path) -> dict[str, float | int]:
    max_mtime = 0.0
    total_size = 0
    file_count = 0
    for root, _, files in os.walk(directory):
        for name in files:
            try:
                st = (Path(root, name)).stat()
                max_mtime = max(max_mtime, st.st_mtime)
                total_size += st.st_size
                file_count += 1
            except OSError:
                continue
    return {"max_mtime": max_mtime, "total_size": total_size, "file_count": file_count}


def sanitize_name(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


def split_file(filepath: Path, max_size: int, temp_dir: Path) -> list[Path]:
    file_size = filepath.stat().st_size
    if file_size <= max_size:
        return [filepath]

    CHUNK = 8 * 1024 * 1024
    parts: list[Path] = []
    part_num = 1
    with open(filepath, "rb") as src:
        while True:
            part_path = temp_dir / f"{filepath.name}.part{part_num}"
            with open(part_path, "wb") as dst:
                written = 0
                while written < max_size:
                    buf = src.read(min(CHUNK, max_size - written))
                    if not buf:
                        break
                    dst.write(buf)
                    written += len(buf)
            if written == 0:
                part_path.unlink(missing_ok=True)
                break
            parts.append(part_path)
            part_num += 1
    return parts


# ── Telegram Helpers ─────────────────────────────────────────────────

async def wait_for_approval(client: TelegramClient, admin_id: int, message: str, timeout: int = 900) -> bool:
    """Sends a message with Approve/Skip buttons and waits for user response or timeout."""
    logger = logging.getLogger(__name__)

    # Create buttons
    buttons = [
        [Button.inline("Approve", b"approve"), Button.inline("Skip", b"skip")]
    ]

    sent_msg = await client.send_message(admin_id, message, buttons=buttons)

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    @client.on(events.CallbackQuery(chat=admin_id))
    async def callback_handler(event):
        if event.message_id != sent_msg.id or future.done():
            return

        if event.data == b"approve":
            await event.answer("Approved")
            await event.edit(message + "\n\n✅ **Approved**")
            future.set_result(True)
        elif event.data == b"skip":
            await event.answer("Skipped")
            await event.edit(message + "\n\n⏭️ **Skipped**")
            future.set_result(False)

    try:
        approved = await asyncio.wait_for(future, timeout=timeout)
        return approved
    except asyncio.TimeoutError:
        logger.info("Approval timed out after %s seconds.", timeout)
        await sent_msg.edit(message + "\n\n⏰ **Timed out (Skipped)**")
        return False
    finally:
        client.remove_event_handler(callback_handler)


async def get_or_create_topic(client: TelegramClient, group: Any, topic_name: str, state_topic_id: Optional[int]) -> int:
    if state_topic_id:
        return state_topic_id
    logger = logging.getLogger(__name__)
    logger.info("Creating forum topic '%s' …", topic_name)
    result = await client(CreateForumTopicRequest(peer=group, title=topic_name))
    for update in result.updates:
        if isinstance(update, UpdateNewChannelMessage):
            return update.message.id
    raise RuntimeError(f"Could not determine topic ID for '{topic_name}'")


async def upload_files(
    client: TelegramClient,
    group: Any,
    topic_id: int,
    file_paths: list[Path],
    *,
    desc: str = "",
) -> list[int]:
    msg_ids: list[int] = []
    for fp in file_paths:
        size = fp.stat().st_size
        label = f"{desc}{fp.name}"
        with tqdm(total=size, desc=label, unit="B", unit_scale=True, unit_divisor=1024, leave=False) as pbar:

            def callback(sent: int, total: int) -> None:
                pbar.total = total
                pbar.update(sent - pbar.n)

            msg = await client.send_file(group, str(fp), reply_to=topic_id, progress_callback=callback)
            msg_ids.append(msg.id)
    return msg_ids


async def send_summary(client: TelegramClient, group: Any, results: dict) -> None:
    lines = [f"✅ Auto-Backup Complete — {datetime.now():%Y-%m-%d %H:%M}"]
    if results.get("projects"):
        lines.append(f"📁 Projects: {results['projects']} backed up")
    if results.get("skipped"):
        lines.append(f"⏭️ Skipped: {results['skipped']} projects")
    if results["errors"]:
        lines.append(f"\n⚠️ Errors ({len(results['errors'])}):")
        for e in results["errors"]:
            lines.append(f"  • {e}")
    else:
        lines.append("\n✅ No errors")
    try:
        await client.send_message(group, "\n".join(lines))
    except Exception:
        logging.getLogger(__name__).warning("Failed to send summary", exc_info=True)


# ── Pipeline A: Projects ─────────────────────────────────────────────

async def backup_projects(client: TelegramClient, config: dict, state: dict, results: dict, group: Any) -> None:
    logger = logging.getLogger(__name__)
    projects_dir: Path = config["projects_dir"]
    results.setdefault("skipped", 0)
    if not projects_dir.is_dir():
        logger.warning("Projects directory not found: %s", projects_dir)
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="backup_projects_"))
    try:
        for project_name in sorted(os.listdir(projects_dir)):
            project_path = projects_dir / project_name
            if not project_path.is_dir():
                continue

            try:
                current_snapshot = get_directory_snapshot(project_path)
                proj_state = state["projects"].get(project_name, {
                    "topic_id": None, "last_snapshot": None, "backups": [],
                })

                if current_snapshot == proj_state.get("last_snapshot"):
                    logger.info("Skipping '%s': no changes detected.", project_name)
                    continue

                logger.info("Backing up '%s' …", project_name)
                safe_name = sanitize_name(project_name)
                archive_path = temp_dir / f"{safe_name}_{datetime.now():%Y%m%d}.tar.gz"

                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(project_path, arcname=project_path.name)

                parts = split_file(archive_path, config["max_file_size"], temp_dir)
                total_size = archive_path.stat().st_size
                total_size_gb = total_size / (1024 ** 3)

                topic_id_state = proj_state.get("topic_id")
                topic_name = f"Backup: {project_name}"

                if not topic_id_state:
                    approval_msg = (
                        f"📁 **New Project Backup**\n\n"
                        f"Project: `{project_name}`\n"
                        f"Action: Create topic and upload files\n"
                        f"Total size: {total_size_gb:.2f} GB"
                    )
                else:
                    approval_msg = (
                        f"📂 **Project Backup**\n\n"
                        f"Project: `{project_name}`\n"
                        f"Topic: `{topic_name}` (ID: {topic_id_state})\n"
                        f"Action: Upload files\n"
                        f"Total size: {total_size_gb:.2f} GB"
                    )

                approved = await wait_for_approval(client, config["admin_user_id"], approval_msg)
                if not approved:
                    logger.info("Skipping '%s' as requested by user.", project_name)
                    results["skipped"] += 1
                    continue

                topic_id = await get_or_create_topic(
                    client, group,
                    topic_name,
                    topic_id_state,
                )
                proj_state["topic_id"] = topic_id
                msg_ids = await upload_files(
                    client, group, topic_id, parts,
                    desc=f"[{project_name}] ",
                )

                date_str = datetime.now().strftime("%Y-%m-%d")
                proj_state["backups"].append({"date": date_str, "message_ids": msg_ids})
                proj_state["last_snapshot"] = current_snapshot

                limit = config["retention_limit"]
                if len(proj_state["backups"]) > limit:
                    oldest = proj_state["backups"].pop(0)
                    await client.delete_messages(group, oldest["message_ids"])
                    logger.info("Deleted old backup for '%s'.", project_name)

                state["projects"][project_name] = proj_state
                results["projects"] += 1

            except Exception as e:
                logger.error("Failed to backup '%s': %s", project_name, e, exc_info=True)
                results["errors"].append(f"Projects/{project_name}: {e}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)



# ── Main ─────────────────────────────────────────────────────────────

async def main() -> None:
    config = load_config()
    setup_logging(config["log_file"])
    logger = logging.getLogger(__name__)

    state = load_state(config["state_file"])
    results: dict[str, Any] = {"projects": 0, "errors": []}

    async with TelegramClient(config["session_file"], config["api_id"], config["api_hash"]) as client:
        try:
            await client.start(bot_token=config["bot_token"])
            logger.info("Telegram client started.")
        except Exception as e:
            logger.critical("Failed to start Telegram client: %s", e, exc_info=True)
            return

        try:
            group = await client.get_entity(config["group_chat_id"])
        except Exception as e:
            logger.critical("Failed to resolve group entity: %s", e, exc_info=True)
            return

        try:
            await backup_projects(client, config, state, results, group)
        except Exception as e:
            logger.critical("Unexpected fatal error: %s", e, exc_info=True)
            results["errors"].append(str(e))
        else:
            logger.info("All pipelines completed successfully.")
        finally:
            save_state(config["state_file"], state)
            logger.info("State saved.")
            await send_summary(client, group, results)

    logger.info("Client disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
