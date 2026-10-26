# SeaLink

A tiny Windows tray app for [SeaDrive](https://www.seafile.com/en/download/) users: when a new file or folder finishes uploading to your Seafile server, SeaLink creates its share link and copies it to the clipboard.

No more digging through Explorer and right-clicking to get a link for the video you just exported.

## Features

- **Push-based, no polling.** Listens to the Seafile notification server (the same websocket SeaDrive uses) and reacts to each commit as soon as it lands on the server, so the file is already fully uploaded.
- **Zero config.** Reads the server URL, account and token from your local SeaDrive installation (read-only). No password to enter.
- **Ignores temporary files.** Video editors write intermediate files while rendering (`*.mp4._00_`, `name.12345.67890.m4v`, GUID-named files, `*.tmp`, auto-save folders...). These never get a link; only the final file does.
- **Folders too.** A new folder gets one link for the folder, not one per file inside it.
- **Reuses existing links** instead of creating duplicates. New links have no expiry and no password.
- **Tray menu** with the last 5 files and the last 5 folders. Each one has *Copy link*, *Open online* and *Show in Explorer*. Left-clicking the tray icon copies the latest link.
- **Catches up** on uploads that happened while SeaLink wasn't running.
- **Follows Windows.** Uses the Windows display language (English and Italian for now, English otherwise) and the light/dark theme for both the tray icon and the menu, and updates live when you switch the theme.

```
Files:
    final_cut.mp4     ▸ Copy link | Open online | Show in Explorer
    poster.png        ▸ …
Folders:
    Client X - photos ▸ Copy link | Open online | Show in Explorer
────────
Status: connected
Pause · Open config · Start with Windows · Quit
```

## Requirements

- Windows 10/11 with SeaDrive 3.x, logged in to your server
- Python 3.10+
- Seafile server 11+ with the **notification server** enabled and reachable from your PC (see below)

## Install

```
pip install -r requirements.txt
pythonw sealink.pyw
```

Then right-click the tray icon and tick **Start with Windows**.

## Notification server

SeaLink needs the Seafile notification server. With the official Docker setup, set `ENABLE_NOTIFICATION_SERVER=true` in `.env` and make it reachable in one of two ways:

- behind your reverse proxy at `https://your.server/notification` (the default when SeaDrive uses an `https://` URL), or
- by publishing the port directly (`ports: - "8083:8083"` in `notification-server.yml`). This is the default when SeaDrive uses an `http://` URL: SeaLink connects to `ws://<host>:8083/`.

To check it, open `http(s)://<notification url>/ping`: it should answer `{"ret": "pong"}`. If your setup is different, set `notification_url` in `config.json`.

## Configuration

`config.json` is created next to the script on first run. You can open it from the tray menu (**Open config**); restart SeaLink after editing it.

| Key | Default | Meaning |
|---|---|---|
| `notification_url` | `""` (auto) | Websocket URL of the notification server |
| `auto_copy` | `true` | Copy the new link to the clipboard |
| `notify` | `true` | Show a Windows notification |
| `menu_items` | `5` | Files/folders shown in the menu |
| `history_size` | `20` | Entries kept in `history.json` |
| `folder_quiet_minutes` | `10` | After a new folder gets its link, files added inside it during this time don't get links of their own |
| `ignore_patterns` | see code | File name patterns (fnmatch, case-insensitive) that never get a link |
| `ignore_dirs` | see code | Folder names whose content is ignored |
| `include_paths` | `[]` | If set, only `"<library>/<path prefix>"` entries get links |
| `exclude_paths` | `[]` | `"<library>/<path prefix>"` entries that never get links |

Logs go to `sealink.log`.

## How it works

1. Gets the list of libraries (`/api2/repos/`) and, for each one, a notification token (`/api/v2.1/repos/<id>/repo-notification-jwt-token/`).
2. Subscribes to them on the notification server websocket.
3. On every `repo-update` event, reads the commit changes (`/api2/repo_history_changes/<id>/?commit_id=`). It looks at added files, added folders, and renames that change the file name: a temp file renamed to its final name counts, a plain move doesn't.
4. Filters out temp files, checks the item still exists, then reuses or creates a share link (`/api/v2.1/share-links/`).

## Limitations

- Windows only (clipboard, tray, registry and SeaDrive paths).
- Encrypted libraries are skipped.
- Uses the most recently used SeaDrive account.

## License

MIT
