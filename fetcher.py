"""
YouTube/fetcher.py — yt-dlp wrapper for video metadata, thumbnails,
channel listing, and stream-URL extraction.

All YouTube interaction goes through this module so the rest of the
codebase never imports yt-dlp directly.
"""

import hashlib
import os
import re
import datetime
import urllib.request

import yaml
import yt_dlp


HASH_ALGORITHM = 'blake2b-128'


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _blake2b(content: bytes) -> str:
    return hashlib.blake2b(content, digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# Front-matter helpers  (mirrors WebSearch/crawler.py)
# ---------------------------------------------------------------------------

def build_frontmatter(meta: dict) -> str:
    lines = ['---']
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f'{key}: {value}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    yaml_block = text[4:end]
    body = text[end + 4:]
    if body.startswith('\n'):
        body = body[1:]
    try:
        meta = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError:
        return {}, text
    return meta, body


# ---------------------------------------------------------------------------
# .channel.yaml helpers  (mirrors WebSearch .crawl.yaml pattern)
# ---------------------------------------------------------------------------

def write_channel_yaml(folder: str, channel_id: str, channel_url: str,
                       channel_name: str = ''):
    path = os.path.join(folder, '.channel.yaml')
    data = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
    data.update({
        'channel_id': channel_id,
        'channel_url': channel_url,
        'channel_name': channel_name,
        'last_sync': datetime.datetime.utcnow().isoformat(),
    })
    os.makedirs(folder, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, default_flow_style=False)


def read_channel_yaml(folder: str) -> dict | None:
    path = os.path.join(folder, '.channel.yaml')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Sanitise folder / file names
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    name = _UNSAFE_CHARS.sub('_', name).strip().strip('.')
    return name or 'unknown'


# ---------------------------------------------------------------------------
# yt-dlp extraction
# ---------------------------------------------------------------------------

_YDL_BASE = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,
    'no_color': True,
}


def fetch_video_info(url: str) -> dict | None:
    """Extract metadata for a single video.  Returns a dict with keys:
    youtube_id, title, author, channel_id, channel_url, publish_date,
    duration, description, tags, thumbnail_url.
    Returns None on failure.
    """
    opts = {
        **_YDL_BASE,
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        print(f"[YouTube/fetcher] Error extracting {url}: {exc}")
        return None

    if info is None:
        return None

    duration_secs = info.get('duration') or 0
    mins, secs = divmod(int(duration_secs), 60)
    hours, mins = divmod(mins, 60)
    duration_str = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

    upload_date = info.get('upload_date', '')
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    # Pick the best available thumbnail URL
    thumbnail_url = info.get('thumbnail', '')
    if not thumbnail_url:
        thumbs = info.get('thumbnails') or []
        if thumbs:
            thumbnail_url = thumbs[-1].get('url', '')

    return {
        'youtube_id': info.get('id', ''),
        'title': info.get('title', ''),
        'author': info.get('uploader', '') or info.get('channel', ''),
        'channel_id': info.get('channel_id', ''),
        'channel_url': info.get('channel_url', ''),
        'publish_date': upload_date,
        'duration': duration_str,
        'duration_seconds': duration_secs,
        'description': info.get('description', ''),
        'tags': info.get('tags') or [],
        'thumbnail_url': thumbnail_url,
        'view_count': info.get('view_count'),
    }


def fetch_channel_videos(channel_url: str, status_callback=None) -> list[dict]:
    """Return a list of basic video info dicts for all public videos
    on a channel.  Each dict has at least youtube_id, title, url."""
    # Force the /videos tab so yt-dlp returns individual video entries
    # instead of tab-level entries whose id is the channel ID (UC…).
    _KNOWN_TABS = ('/videos', '/shorts', '/live', '/streams', '/releases')
    listing_url = channel_url.rstrip('/')
    if not any(listing_url.endswith(t) for t in _KNOWN_TABS):
        listing_url = listing_url + '/videos'

    opts = {
        **_YDL_BASE,
        'extract_flat': True,
        'playlistend': 5000,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(listing_url, download=False)
    except Exception as exc:
        print(f"[YouTube/fetcher] Channel extraction error: {exc}")
        return []

    if info is None:
        return []

    entries = info.get('entries') or []
    results = []
    for i, entry in enumerate(entries):
        vid_id = entry.get('id', '')
        if not vid_id:
            continue
        # Skip channel-level entries; channel IDs are 24 chars starting with 'UC'
        if len(vid_id) == 24 and vid_id.startswith('UC'):
            print(f"[YouTube/fetcher] Skipping channel-level entry id={vid_id!r}")
            continue
        results.append({
            'youtube_id': vid_id,
            'title': entry.get('title', ''),
            'url': entry.get('url', '') or f'https://www.youtube.com/watch?v={vid_id}',
        })
        if status_callback and (i + 1) % 50 == 0:
            status_callback(f'Listed {i + 1} videos…')

    if status_callback:
        status_callback(f'Found {len(results)} videos.')
    return results


_BOT_PATTERNS = ('sign in to confirm', 'not a bot', '--cookies')

OAUTH_CACHE_SUBDIR = '.yt-dlp-cache'

_DEVICE_URL_RE = re.compile(r'https://www\.google\.com/device\S*')
_DEVICE_CODE_RE = re.compile(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b')


class _OAuth2Logger:
    """yt-dlp logger that captures the OAuth2 device-code URL and code.
    All to_screen() calls go to debug() when a custom logger is provided.
    """
    def __init__(self, event_cb):
        self._cb = event_cb
        self._emitted = False

    def _scan(self, msg: str):
        if self._emitted:
            return
        url_m = _DEVICE_URL_RE.search(msg)
        if not url_m:
            return
        self._emitted = True
        code_m = _DEVICE_CODE_RE.search(msg)
        self._cb('device_code', {
            'url': url_m.group(0).rstrip('.,)'),
            'code': code_m.group(1) if code_m else None,
        })

    def debug(self, msg): self._scan(msg)
    def info(self, msg): self._scan(msg)
    def warning(self, msg): self._scan(msg)
    def error(self, msg): self._cb('error', {'message': msg})


def start_oauth2_flow(cache_dir: str, event_cb) -> dict:
    """Run yt-dlp's YouTube OAuth2 device-code flow.

    *event_cb(event_name, payload)* is called with:
      ('device_code', {'url': ..., 'code': ...})   – when the code is ready
      ('complete',    {'ok': True})                 – on success
      ('complete',    {'ok': False, 'error': ...})  – on failure

    Blocks until authorization completes or fails. Run in a background thread.
    """
    os.makedirs(cache_dir, exist_ok=True)
    logger = _OAuth2Logger(event_cb)
    opts = {
        **_YDL_BASE,
        'quiet': False,   # ignored with custom logger, but explicit
        'username': 'oauth2',
        'password': '',
        'cachedir': cache_dir,
        'logger': logger,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Any YouTube URL triggers the auth check.
            # Video-level errors (unavailable, geo-block) are acceptable.
            ydl.extract_info(
                'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                download=False,
            )
        event_cb('complete', {'ok': True})
        return {'ok': True}
    except Exception as exc:
        err = str(exc)
        if _is_bot_error(err):
            event_cb('complete', {'ok': False, 'error': 'Authorization was not completed.'})
            return {'ok': False, 'error': err}
        # Other errors (geo-block, unavailable video) are fine — auth succeeded.
        event_cb('complete', {'ok': True})
        return {'ok': True}


def _is_bot_error(message: str) -> bool:
    low = message.lower()
    return any(p in low for p in _BOT_PATTERNS)


# Quality → yt-dlp format string mapping.
# All formats prefer a single pre-muxed progressive stream so the browser can
# seek freely and volume/speed controls (including browser extensions) work on
# the single <video> element.  YouTube caps pre-muxed streams at ~720 p for
# most videos; selecting a higher ceiling still delivers the best available
# pre-muxed quality.
QUALITY_FORMATS: dict[str, str] = {
    '2160': 'best[height<=2160][ext=mp4]/best[height<=2160]/best',
    '1440': 'best[height<=1440][ext=mp4]/best[height<=1440]/best',
    '1080': 'best[height<=1080][ext=mp4]/best[height<=1080]/best',
    '720':  'best[height<=720][ext=mp4]/best[height<=720]/best',
    '480':  'best[height<=480][ext=mp4]/best[height<=480]/best',
    '360':  'best[height<=360][ext=mp4]/best[height<=360]/best',
    'best': 'best[ext=mp4]/best',
}


def _build_ydl_opts(cookies_file: str | None, oauth_cache_dir: str | None,
                    extra: dict | None = None) -> dict:
    opts = {**_YDL_BASE, **(extra or {})}
    if cookies_file and os.path.isfile(cookies_file):
        opts['cookiefile'] = cookies_file
    elif oauth_cache_dir and os.path.isdir(oauth_cache_dir) and \
            any(True for _ in os.scandir(oauth_cache_dir)):
        opts['username'] = 'oauth2'
        opts['password'] = ''
        opts['cachedir'] = oauth_cache_dir
    return opts


def get_stream_urls(youtube_id: str, quality: str = '1080',
                    cookies_file: str | None = None,
                    oauth_cache_dir: str | None = None) -> dict:
    """Extract a direct stream URL for a video at the requested quality.

    Returns one of:
      {'url': ..., 'ext': ..., 'height': ...}   – pre-muxed (seekable)
      {'error': ..., 'needs_cookies': True}      – auth required
      {'error': ...}                             – other failure
    """
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS['1080'])
    url = f'https://www.youtube.com/watch?v={youtube_id}'
    opts = _build_ydl_opts(cookies_file, oauth_cache_dir, {'format': fmt})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            return {'error': 'Could not extract video info'}
        stream_url = info.get('url') or ''
        if not stream_url:
            fmts = info.get('requested_formats') or []
            stream_url = fmts[0].get('url', '') if fmts else ''
        if not stream_url:
            return {'error': 'No stream URL returned by yt-dlp'}
        return {'url': stream_url, 'ext': info.get('ext', 'mp4'), 'height': info.get('height')}
    except Exception as exc:
        err = str(exc)
        if _is_bot_error(err):
            return {'error': err, 'needs_cookies': True}
        return {'error': err}


def extract_stream_url(youtube_id: str, cookies_file: str | None = None,
                       oauth_cache_dir: str | None = None) -> dict:
    """Get a direct playable stream URL for a video (legacy helper; prefer get_stream_urls).
    Returns {'url': ..., 'ext': ..., 'height': ...} or {'error': ..., 'needs_cookies': True}.
    """
    return get_stream_urls(youtube_id, quality='1080',
                           cookies_file=cookies_file, oauth_cache_dir=oauth_cache_dir)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def _download_thumbnail(thumbnail_url: str, dest_path: str):
    """Download a thumbnail image to *dest_path*."""
    try:
        req = urllib.request.Request(thumbnail_url, headers={
            'User-Agent': 'Mozilla/5.0',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, 'wb') as f:
            f.write(data)
    except Exception as exc:
        print(f"[YouTube/fetcher] Thumbnail download failed: {exc}")


def store_video(storage_dir: str, video_info: dict,
                subfolder: str | None = None) -> dict:
    """Write .link + .link.preview.png + .link.meta for a video.

    *video_info* is the dict returned by ``fetch_video_info()``.
    *subfolder* overrides the channel folder name.

    Returns {'file_path': relative_path, 'hash': blake2b_hash} or
    {'error': ...} on failure.
    """
    vid_id = video_info.get('youtube_id', '')
    if not vid_id:
        return {'error': 'No youtube_id'}

    # Determine channel subfolder
    channel_name = subfolder or _safe_name(video_info.get('author', '') or 'uncategorized')
    folder = os.path.join(storage_dir, channel_name)
    os.makedirs(folder, exist_ok=True)

    # Write .channel.yaml if it doesn't exist yet
    if video_info.get('channel_id') and not os.path.exists(os.path.join(folder, '.channel.yaml')):
        write_channel_yaml(
            folder,
            channel_id=video_info['channel_id'],
            channel_url=video_info.get('channel_url', ''),
            channel_name=video_info.get('author', ''),
        )

    # Build frontmatter
    safe_title = (video_info.get('title', '') or '').replace('"', '\\"')
    safe_author = (video_info.get('author', '') or '').replace('"', '\\"')
    description = video_info.get('description', '') or ''
    preview_text = description[:300].replace('"', '\\"')
    if len(description) > 300:
        preview_text += '…'

    fm = build_frontmatter({
        'url': f'https://www.youtube.com/watch?v={vid_id}',
        'youtube_id': vid_id,
        'title': safe_title,
        'author': safe_author,
        'channel_id': video_info.get('channel_id', ''),
        'publish_date': video_info.get('publish_date', ''),
        'duration': video_info.get('duration', ''),
        'duration_seconds': video_info.get('duration_seconds', 0),
        'preview': preview_text,
    })

    # Write .link file
    link_filename = f'{vid_id}.link'
    link_path = os.path.join(folder, link_filename)
    with open(link_path, 'w', encoding='utf-8') as f:
        f.write(fm)

    file_hash = _blake2b(fm.encode('utf-8'))

    # Write .link.meta (full description + tags for semantic search)
    meta_path = link_path + '.meta'
    meta_parts = []
    if video_info.get('title'):
        meta_parts.append(video_info['title'])
    if video_info.get('author'):
        meta_parts.append(f"Channel: {video_info['author']}")
    if description:
        meta_parts.append(description)
    if video_info.get('tags'):
        meta_parts.append('Tags: ' + ', '.join(video_info['tags']))
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(meta_parts))

    # Download thumbnail
    thumb_url = video_info.get('thumbnail_url', '')
    if thumb_url:
        preview_path = link_path + '.preview.png'
        _download_thumbnail(thumb_url, preview_path)

    rel_path = os.path.relpath(link_path, storage_dir)
    return {'file_path': rel_path, 'hash': file_hash}


def find_channel_folder(storage_dir: str, channel_id: str) -> str | None:
    """Find an existing channel folder by channel_id in .channel.yaml files."""
    try:
        for entry in os.scandir(storage_dir):
            if not entry.is_dir():
                continue
            conf = read_channel_yaml(entry.path)
            if conf and conf.get('channel_id') == channel_id:
                return entry.name
    except OSError:
        pass
    return None
