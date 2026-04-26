# YouTube Module - Changelog

### Version 0.3.14.4 [for Anagnorisis ≥ 0.3.14] (26.04.2026)
*   **Thumbnail compression**
    *   Thumbnails are now saved as JPEG (quality 85) instead of PNG, resized to at most 512 px on the longest side. This reduces per-video preview size significantly.
    *   A startup task converts all existing `.preview.png` files in the storage directory to the new format and removes the originals.

### Version 0.3.13.3 [for Anagnorisis ≥ 0.3.13] (20.04.2026)
*   **Transcripts**
    *   Subtitles are now fetched for every newly imported video and appended to its `.meta` file with timestamps (e.g. `[1:23] text`). Manual subtitles in the video's original language are preferred; auto-generated captions are used as a fallback.
    *   A scheduled backfill task runs every 40 minutes and fetches transcripts for already-stored videos that don't have them yet, processing up to 50 videos per cycle.
    *   Configurable via `transcript_backfill_interval_minutes` and `transcript_backfill_batch_size` in config.

*   **Channel folder naming**
    *   Channel folders now include a short channel ID suffix (e.g. `Action Lab (UCxxxxxx)`) to prevent name collisions between channels with identical or very similar names.
    *   Existing folders without the suffix are automatically renamed on startup and all affected database paths are updated.

*   **Richer embeddings and scoring**
    *   Semantic search, model scoring, and training now use both the `.link` front-matter and the `.meta` sidecar as a single combined text. Previously only `.meta` was used, leaving out structured fields like title, channel name, and publish date.

### Version 0.3.13.2 [for Anagnorisis ≥ 0.3.13] (14.04.2026)
*   **Scheduled channel sync**
    *   `auto_update` boolean flag added to `.channel.yaml`. Only channels imported via "**+ Add channel**" have `auto_update: true`; videos added individually or through a playlist get `auto_update: false`. Channels written before this field existed default to `true` for backward compatibility. The flag can be toggled manually in the file.

### Version 0.3.13.1 [for Anagnorisis ≥ 0.3.13] (13.04.2026)

*   **JS runtime** 
    *    Node.js 20 is now installed in the Docker image via NodeSource (Ubuntu's default v12 was below yt-dlp's v20 minimum). `js_runtimes: {node: {}}` and `remote_components: {ejs:github}` added to the base yt-dlp options so YouTube JS challenges are solved automatically.

*   **Authentication** 
    *    OAuth2 device-code flow was removed (dropped by yt-dlp 2026.03.17). Authentication is now done by uploading a `cookies.txt` file (Netscape format) via a drag-and-drop modal. The file is saved to the storage directory and the stream-URL cache is cleared on upload.

*   **Playlist import**
    *    new "**+ Add from playlist**" sidebar button and modal. Accepts any `youtube.com/playlist?list=…` URL, lists all videos via yt-dlp flat extraction, and imports them using the same per-channel subfolder logic as the channel importer. Already-stored videos are skipped. Progress is shown in the sidebar status line.

*   **Scheduled channel sync**
    *   New background task that automatically checks every tracked channel for newly published videos on a configurable interval (default: every 30 minutes).
    *   Efficient API usage, 1 flat-extraction call per channel to retrieve the N most-recent video stubs (default `N=15`, configurable). Already-stored videos are detected with a fast filesystem check (no network round-trip). A full metadata fetch is made only for genuinely new videos.
    *   Channel folders are discovered by scanning for `.channel.yaml` files in the storage directory. The `last_sync` timestamp in each `.channel.yaml` is refreshed after every sync cycle.
    *   Configurable via `config.defaults.yaml` (or the `YouTube:` section in the root `config.yaml`):
        *   `channel_update_interval_minutes` (default `720`) - set to `0` to disable.
        *   `channel_update_recent_count` (default `50`) - how many recent videos to inspect per channel per cycle.

*   **Datetime serialization**
    *   `YoutubeLibrary.as_dict()` now converts `DateTime` column values to ISO 8601 strings, preventing `TypeError: Object of type datetime is not JSON serializable` when the socket event emitted a video record that contained a `user_rating_date` or `last_viewed` value.


### Version 0.3.13.0 [for Anagnorisis ≥ 0.3.13] (12.04.2026)
Initial implementation.

*   **Core architecture**
    *   Videos are represented locally as `.link` files (YAML front-matter) rather than downloaded media. YouTube is used purely as a streaming CDN.
    *   Each video entry stores: `youtube_id`, `title`, `author`, `channel_id`, `publish_date`, `duration`, `duration_seconds`, and a short `preview` excerpt from the description.
    *   Rich metadata for semantic search is written to a sidecar `.link.meta` file (full description + tags).
    *   Thumbnails are downloaded once at import time as `.link.preview.png`.
    *   Channels are tracked with a `.channel.yaml` file inside each channel subfolder, storing `channel_id`, `channel_url`, `channel_name`, and `last_sync` timestamp.

*   **Backend (`serve.py`, `fetcher.py`)**
    *   `_YoutubeTextEngine` adapter bridges `FileManager` / `CommonFilters` to the `.link` file format, providing hashing, front-matter reading, and text embedding over `.meta` sidecar files.
    *   `FileManager` and `CommonFilters` (shared infrastructure) handle pagination, sorting, filtering, and semantic search identically to other modules.
    *   Semantic search (`semantic-metadata` mode) embeds the `.link.meta` content directly - no vision model or auto-description is ever invoked.
    *   Supported sort/filter modes: `file-name` (fuzzy title match), `semantic-metadata`, `rating`, `recommendation`, `recent`, `random`.
    *   Stream proxy: `/youtube_stream/<id>?quality=<q>` fetches a pre-muxed progressive URL via yt-dlp and proxies it to the browser with full HTTP Range support, enabling free seeking and correct total-duration display without downloading the video.
    *   yt-dlp stream URLs are cached in-process for 4 hours to avoid redundant API calls.
    *   Quality selector: 360p / 480p / 720p / 1080p / 1440p / 4K. YouTube caps pre-muxed streams at ~720p for most videos; higher settings deliver the best available pre-muxed quality.
    *   Authentication: OAuth2 device-code flow (`start_oauth2_flow`) and `cookies.txt` (Netscape format) are both supported. Token cached under `.yt-dlp-cache/` inside the storage directory.
    *   `last_viewed` timestamp updated in the DB when playback begins.
    *   Model ratings computed by the universal evaluator and stored in `YoutubeLibrary` DB table (same schema pattern as other modules); auto-rescoring triggered when the evaluator changes.
    *   All long-running operations (add video, add channel, channel sync) run through the centralised `TaskManager`.

*   **Frontend (`page.html`, `js/main.js`)**
    *   File grid shows thumbnail, title, channel, publish date, duration, last-viewed time (relative, e.g. "3d ago"), user rating, and model rating.
    *   Video player is a native `<video>` element inside a full-screen modal (98 vw × 98 vh). Quality can be changed mid-playback; position is preserved on quality switch.
    *   Right-click context menu: Play, Edit .meta file, Show full search description.
    *   Sidebar: Add video (single URL), Add channel (channel URL with optional initial rating), Sign in to YouTube.
    *   OAuth2 sign-in modal with device-code display and live status updates.
    *   Folder tree (`FolderViewComponent`) for browsing by channel.
    *   Dual pagination (top and bottom), search bar with mode and order controls.
