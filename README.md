# Auto-Backup

A Python-based Telegram bot that automatically backs up local project directories to a Telegram group. It monitors a projects directory, detects changes via file modification timestamps, sizes, and counts, archives changed projects as `.tar.gz` files, and uploads them to a Telegram group — optionally into dedicated forum topics.

## How It Works

1. Loads configuration from environment variables (`.env`).
2. Authenticates with Telegram using a bot token (via Telethon/MTProto).
3. Scans each subdirectory in `PROJECTS_DIR`, computing a snapshot (`max_mtime`, `total_size`, `file_count`).
4. Compares each snapshot with the last known state — unchanged projects are skipped.
5. For changed projects:
   - Creates a `.tar.gz` archive (split into parts if larger than `MAX_FILE_SIZE_GB`).
   - Sends an **Approve / Skip** inline button to the admin via direct message and waits for approval (with timeout).
   - Creates or reuses a forum topic titled `Backup: <project>` in the target group.
   - Uploads the archive parts into the topic with progress bars.
   - Enforces retention — deletes the oldest backups exceeding `RETENTION_LIMIT`.
6. Saves updated state atomically to `STATE_FILE`.
7. Sends a summary message and disconnects.

## Configuration

All options are set via environment variables in `.env` (see `.env.example`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_ID` | Yes | — | Telegram app API ID (from my.telegram.org) |
| `API_HASH` | Yes | — | Telegram app API hash |
| `BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `ADMIN_USER_ID` | Yes | — | Numeric user ID of the admin receiving approval prompts |
| `GROUP_CHAT_ID` | Yes | — | Numeric ID of the target group (negative for supergroups) |
| `STATE_FILE` | Yes | — | Path to the persistent state JSON file |
| `PROJECTS_DIR` | Yes | — | Path to the directory containing project subfolders to back up |
| `SESSION_FILE` | No | `backup_session` | Telethon session file path (auth cache) |
| `LOG_FILE` | No | (stderr only) | Path to a log file |
| `MAX_FILE_SIZE_GB` | No | `1.9` | Max archive size (GB) before splitting |
| `RETENTION_LIMIT` | No | `10` | Max backups to keep per project; oldest are auto-deleted |

## Usage

```bash
pip install -r requirements.txt
python main.py
```
