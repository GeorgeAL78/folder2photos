# gclone

Watches a folder and uploads new photos and videos to Google Photos, all from a web interface.

- **Each file is uploaded once.** gclone keeps its own history (SQLite in `/config`), so a photo you delete in Google Photos is not uploaded again.
- **No duplicates.** Before uploading, gclone fingerprints the file (SHA-1). A copy of a photo that was already uploaded, under another name or in another folder, is skipped and listed under Duplicates.
- **Only new files.** On first start, only files dated from that day on are uploaded. You can change the date, or mark everything currently in the folder as skipped.
- **Albums.** Uploads go into an album (default `Camera`). Optionally each subfolder becomes its own album, e.g. `Camera/2026-11-08`.
- **Web interface** with dashboard, waiting list, history (uploaded / failed / skipped, with "upload again"), settings, Google sign-in, and log.

Uploads use [rclone](https://rclone.org/googlephotos/)'s Google Photos backend.

## Run

```bash
docker run -d --name gclone -p 8080:8080 \
  -v /mnt/user/appdata/gclone:/config \
  -v "/mnt/user/share/Photos & Videos/a6600":/photos:ro \
  -e TZ=America/New_York \
  gjergjk/gclone:latest
```

Open `http://SERVER:8080`. On Unraid, install **gclone** from Community Applications.

| Path / variable | Purpose |
|---|---|
| `/config` | Settings, upload history, `rclone.conf` with the Google login |
| `/photos` | The folder to watch (read only is fine) |
| `TZ` | Time zone for dates and the schedule |
| `WEB_PASSWORD` | Optional password for the web interface |

## Connect Google Photos

Google requires your own OAuth client:

1. In Google Cloud Console, enable the **Photos Library API**.
2. In **Google Auth Platform → Data Access**, add the scopes
   `photoslibrary.appendonly`, `photoslibrary.readonly.appcreateddata`, `photoslibrary.edit.appcreateddata`.
3. In **Audience**, publish the app (otherwise the login expires after 7 days).
4. In **Clients**, create a **Desktop app** client.

Then in gclone open **Google**, enter the Client ID and secret, click **Sign in with Google**, allow access, and paste the address of the page Google sends you to (it starts with `http://127.0.0.1:53682/` and does not load; that is expected). You can also import an existing `rclone.conf`.

Source and issues: https://github.com/GeorgeAL78/gclone
