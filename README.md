# ShutterLift

Watches a folder and uploads new photos and videos to Google Photos, all from a web interface.

- **Each file is uploaded once.** ShutterLift keeps its own history (SQLite in `/config`), so a photo you delete in Google Photos is not uploaded again.
- **No duplicates.** Before uploading, ShutterLift fingerprints the file (SHA-1). A copy of a photo that was already uploaded, under another name or in another folder, is skipped and listed under Duplicates.
- **Only new files.** On first start, only files dated from that day on are uploaded. You can change the date, or mark everything currently in the folder as skipped.
- **Albums from subfolders.** Photos directly in the folder go to your Google Photos library without an album. Photos in a subfolder go to an album with the subfolder's name, e.g. `Hike 2026`. (Can be turned off.)
- **API and MCP** so scripts and AI assistants can check status, start uploads and change settings.
- **Web interface** with dashboard, waiting list, history (uploaded / failed / skipped, with "upload again"), settings, Google sign-in, and log.

Uploads use [rclone](https://rclone.org/googlephotos/)'s Google Photos backend.

## Run

```bash
docker run -d --name shutterlift -p 8080:8080 \
  -v /mnt/user/appdata/shutterlift:/config \
  -v "/mnt/user/share/Photos & Videos/a6600":/photos:ro \
  -e TZ=America/New_York \
  gjergjk/shutterlift:latest
```

Open `http://SERVER:8080`. On Unraid, install **ShutterLift** from Community Applications.

| Path / variable | Purpose |
|---|---|
| `/config` | Settings, upload history, `rclone.conf` with the Google login |
| `/photos` | The folder to watch (read only is fine) |
| `TZ` | Time zone for dates and the schedule |
| `WEB_PASSWORD` | Optional password for the web interface |
| `API_TOKEN` | Optional fixed API token (otherwise one is generated; see the API tab) |

## API and AI assistants

ShutterLift can be controlled by scripts and AI assistants. The **API** tab in the web interface shows the API token and ready-to-copy examples. Every API and MCP request needs the header `Authorization: Bearer <token>`. You can set your own token with the `API_TOKEN` variable.

**MCP** (for Claude and other MCP clients) is at `/mcp` (streamable HTTP). Tools: `get_status`, `list_pending`, `list_history`, `upload_now`, `stop_upload`, `retry_failed`, `upload_again`, `skip_existing_files`, `get_settings`, `update_settings`, `get_log`, `check_google`.

```bash
claude mcp add --transport http shutterlift http://SERVER:8080/mcp --header "Authorization: Bearer <token>"
```

**REST** endpoints are under `/api/v1`; the full list is at `/api/v1/openapi.json`.

| Method | Path | Does |
|---|---|---|
| GET | `/api/v1/status` | Running or idle, progress, counts, Google connection, next run |
| GET | `/api/v1/pending` | Files waiting to upload |
| GET | `/api/v1/files?status=uploaded` (or failed, skipped, duplicate; `q=` to search) | History |
| POST | `/api/v1/upload` | Start an upload now |
| POST | `/api/v1/stop` | Stop after the current file |
| POST | `/api/v1/files/forget` | Upload these paths again (`{"paths": [...]}`) |
| POST | `/api/v1/files/retry-failed` | Retry failed uploads |
| POST | `/api/v1/skip-existing` | Skip everything currently in the folder |
| GET / PATCH | `/api/v1/settings` | Read or change settings |
| GET | `/api/v1/logs` | Recent activity |
| POST | `/api/v1/google/check` | Test the Google connection |

## Connect Google Photos

Google requires your own OAuth client:

1. In Google Cloud Console, enable the **Photos Library API**.
2. In **Google Auth Platform → Data Access**, add the scopes
   `photoslibrary.appendonly`, `photoslibrary.readonly.appcreateddata`, `photoslibrary.edit.appcreateddata`.
3. In **Audience**, publish the app (otherwise the login expires after 7 days).
4. In **Clients**, create a **Desktop app** client.

Then in ShutterLift open **Google**, enter the Client ID and secret, click **Sign in with Google**, allow access, and paste the address of the page Google sends you to (it starts with `http://127.0.0.1:53682/` and does not load; that is expected). You can also import an existing `rclone.conf`.
