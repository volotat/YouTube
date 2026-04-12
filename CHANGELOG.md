# YouTube Module - Changelog

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
    *   Semantic search (`semantic-metadata` mode) embeds the `.link.meta` content directly — no vision model or auto-description is ever invoked.
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
