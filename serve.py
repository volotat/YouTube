"""
YouTube module — serve.py

Local YouTube browsing: videos stored as .link files with YAML front-matter,
thumbnails as .link.preview.jpg, rich metadata in .link.meta.  Videos stream
via yt-dlp extracted direct URLs in a native <video> player (no YouTube UI).
"""

import datetime
import os
import re
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import rapidfuzz.fuzz
from flask import Flask, Response, jsonify, send_from_directory
from flask import request as flask_request
from flask_socketio import SocketIO
from omegaconf import OmegaConf

import modules.YouTube.db_models as db_models
import modules.YouTube.tasks as tasks
import src.file_manager as file_manager
from modules.YouTube.engine import YoutubeTextEngine, get_text_preview
from modules.YouTube.fetcher import (
    HASH_ALGORITHM,
    fetch_channel_videos,
    fetch_playlist_videos,
    fetch_video_info,
    get_stream_urls,
    read_channel_yaml,
    read_video_text,
    store_video,
)
from modules.train.universal_train import UniversalEvaluator
from src.caching import TwoLevelCache
from src.common_filters import CommonFilters, _normalize_text
from src.module_helpers import register_meta_handlers
from src.recommendation_engine import sort_files_by_recommendation
from src.scheduler import schedule_task
from src.socket_events import CommonSocketEvents
from src.text_embedder import TextEmbedder

# ── Folder-tree helpers ────────────────────────────────────────────────────────

# Channel folders are named "Channel Name (UCxxxxxx)" — 4+ alphanum chars in
# trailing parentheses — as produced by make_channel_folder_name().
_CHANNEL_FOLDER_RE = re.compile(r' \([A-Za-z0-9_-]{4,}\)$')


def _sort_folder_tree(node: dict) -> dict:
    """Recursively sort subfolders: category folders first, channel folders second,
    each group case-insensitively alphabetical.
    """
    subs = node.get('subfolders')
    if not subs:
        return node
    is_channel = _CHANNEL_FOLDER_RE.search
    categories = sorted((k for k in subs if not is_channel(k)), key=str.casefold)
    channels   = sorted((k for k in subs if     is_channel(k)), key=str.casefold)
    node['subfolders'] = {k: _sort_folder_tree(subs[k]) for k in categories + channels}
    return node

# ── Stream URL cache (module-level so all requests share it) ─────────────────

_stream_url_cache: dict = {}
_stream_url_lock = threading.Lock()


def _get_or_fetch_urls(storage_dir: str, youtube_id: str, quality: str) -> dict:
    """Return cached yt-dlp stream URLs, or fetch and cache fresh ones (~4 h TTL)."""
    cache_key = f'{youtube_id}:{quality}'
    now = time.time()
    with _stream_url_lock:
        cached = _stream_url_cache.get(cache_key)
        if cached and cached.get('_expires', 0) > now:
            return cached
    cookies_path = os.path.join(storage_dir, 'cookies.txt')
    result = get_stream_urls(
        youtube_id,
        quality=quality,
        cookies_file=cookies_path if os.path.isfile(cookies_path) else None,
    )
    if 'error' not in result:
        result['_expires'] = now + 4 * 3600
        with _stream_url_lock:
            _stream_url_cache[cache_key] = result
    return result


def _proxy_cdn(target_url: str) -> Response:
    """Proxy a YouTube CDN URL, forwarding Range headers so seeking works."""
    headers = {'User-Agent': 'Mozilla/5.0'}
    range_hdr = flask_request.headers.get('Range')
    if range_hdr:
        headers['Range'] = range_hdr
    req = urllib.request.Request(target_url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        return Response(exc.read(), status=exc.code, content_type='text/plain')
    resp_headers = {
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-cache',
        'Content-Type': resp.headers.get('Content-Type', 'video/mp4'),
    }
    if cl := resp.headers.get('Content-Length'):
        resp_headers['Content-Length'] = cl
    if cr := resp.headers.get('Content-Range'):
        resp_headers['Content-Range'] = cr
    status = 206 if 'Content-Range' in resp_headers else 200

    def _generate():
        try:
            while chunk := resp.read(65536):
                yield chunk
        finally:
            resp.close()

    return Response(_generate(), status=status, headers=resp_headers)


# ── Per-request filter functions (module-level, engine passed explicitly) ─────

def _filter_fuzzy_title(engine, files, text_query, **__):
    q = _normalize_text(text_query)
    q_raw = text_query.strip().lower()
    scorer = rapidfuzz.fuzz.token_set_ratio if ' ' in q else rapidfuzz.fuzz.WRatio
    scores = []
    for f in files:
        title, url = engine.get_title_and_url(f)
        s_title = scorer(q, _normalize_text(title))
        s_url = scorer(q, _normalize_text(url))
        combined = max(1.3 * s_title, s_url)
        priority = 3 if q_raw and q_raw in title.lower() else (
                   2 if q_raw and q_raw in url.lower() else 0)
        scores.append(priority * 10.0 + combined)
    return np.array(scores, dtype=np.float32) / 100.0


def _filter_recommendation(engine, files, *_, **__):
    files_data = []
    for f in files:
        video_id = engine.get_video_id(f)
        db_item = db_models.YoutubeLibrary.query.filter_by(url=video_id).first() if video_id else None
        files_data.append({
            'user_rating':     db_item.user_rating  if db_item else None,
            'model_rating':    db_item.model_rating if db_item else None,
            'full_play_count': 1,
            'skip_count':      0,
            'last_played':     db_item.last_viewed  if db_item else None,
        })
    return np.array(sort_files_by_recommendation(files, files_data), dtype=np.float32)


def _filter_recent(engine, files, *_, **__):
    ts = []
    for f in files:
        meta = engine.get_frontmatter(f)
        try:
            dt = datetime.datetime.fromisoformat(str(meta.get('publish_date', '')))
            ts.append(dt.timestamp())
        except Exception:
            ts.append(0.0)
    ts = np.array(ts, dtype=np.float32)
    rng = ts.max() - ts.min() if len(ts) else 0
    return (ts - ts.min()) / (rng + 1e-8)


def init_socket_events(socketio: SocketIO, app: Flask = None, cfg=None,
                       data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="YouTube")

    # ── Config ───────────────────────────────────────────────────────────
    storage_dir = OmegaConf.select(cfg, 'YouTube.storage_directory',
                                   default='/mnt/project_config/modules/YouTube')
    os.makedirs(storage_dir, exist_ok=True)

    score_interval               = int(OmegaConf.select(cfg, 'YouTube.score_interval_minutes',               default=15))
    channel_update_interval      =     OmegaConf.select(cfg, 'YouTube.channel_update_interval_minutes',     default=720)
    channel_update_count         = int(OmegaConf.select(cfg, 'YouTube.channel_update_recent_count',         default=50))
    transcript_backfill_interval =     OmegaConf.select(cfg, 'YouTube.transcript_backfill_interval_minutes', default=40)
    transcript_backfill_batch    = int(OmegaConf.select(cfg, 'YouTube.transcript_backfill_batch_size',      default=50))

    # ── Cache + embedder + engine ─────────────────────────────────────────
    yt_cache = TwoLevelCache(
        cache_dir=os.path.join(cfg.main.cache_path, 'YouTube'),
        name='youtube_embeddings',
    )
    common_socket_events.show_loading_status('Initializing text embedder for YouTube…')
    text_embedder = TextEmbedder(cfg=cfg)
    text_embedder.initiate(models_folder=cfg.main.embedding_models_path)
    engine = YoutubeTextEngine(text_embedder, yt_cache, storage_dir, cfg=cfg)

    # ── File manager + filters ────────────────────────────────────────────
    common_socket_events.show_loading_status('Setting up YouTube file manager…')
    yt_file_manager = file_manager.FileManager(
        cfg=cfg,
        media_directory=storage_dir,
        engine=engine,
        module_name='YouTube',
        media_formats={'.link'},
        socketio=socketio,
        db_schema=db_models.YoutubeLibrary,
    )
    yt_filters = CommonFilters(
        engine=engine,
        metadata_engine=engine,
        common_socket_events=common_socket_events,
        media_directory=storage_dir,
        db_schema=db_models.YoutubeLibrary,
        update_model_ratings_func=None,  # wired below after deps is built
    )

    # ── Evaluator ─────────────────────────────────────────────────────────
    common_socket_events.show_loading_status('Loading universal evaluator for YouTube…')
    evaluator = UniversalEvaluator()
    evaluator_path = os.path.join(cfg.main.personal_models_path, 'universal_evaluator.pt')
    if os.path.exists(evaluator_path):
        evaluator.load(evaluator_path)
    else:
        print('[YouTube] universal_evaluator.pt not found – model scoring disabled.')

    # ── Task deps ─────────────────────────────────────────────────────────
    deps = tasks.Deps(
        app=app,
        storage_dir=storage_dir,
        evaluator=evaluator,
        text_embedder=text_embedder,
        engine=engine,
        channel_update_recent_count=channel_update_count,
        transcript_backfill_batch=transcript_backfill_batch,
    )
    yt_filters.update_model_ratings_func = lambda fps: tasks.update_model_ratings(fps, deps)
    task_manager = app.task_manager

    # ── Filters + file-info (built once, reused per request) ─────────────
    filters = {
        'by_text':        lambda files, text_query, mode='file-name', **kw: (
                              _filter_fuzzy_title(engine, files, text_query)
                              if mode == 'file-name'
                              else yt_filters.filter_by_text(files, text_query, mode=mode, **kw)),
        'rating':         yt_filters.filter_by_rating,
        'random':         yt_filters.filter_by_random,
        'recommendation': lambda files, *_, **__: _filter_recommendation(engine, files),
        'recent':         lambda files, *_, **__: _filter_recent(engine, files),
    }

    def _get_file_info(full_path, file_hash):
        video_id = engine.get_video_id(full_path)
        db_item = db_models.YoutubeLibrary.query.filter_by(url=video_id).first() if video_id else None
        meta = engine.get_frontmatter(full_path)
        preview_rel = next(
            (os.path.relpath(full_path + ext, storage_dir)
             for ext in ('.preview.jpg', '.preview.png')
             if os.path.exists(full_path + ext)),
            None,
        )
        return {
            'user_rating':      db_item.user_rating  if db_item else None,
            'model_rating':     db_item.model_rating if db_item else None,
            'last_viewed':      db_item.last_viewed.isoformat() if db_item and db_item.last_viewed else None,
            'url':              meta.get('url', ''),
            'youtube_id':       meta.get('youtube_id', ''),
            'title':            meta.get('title', ''),
            'author':           meta.get('author', ''),
            'duration':         meta.get('duration', ''),
            'duration_seconds': meta.get('duration_seconds', 0),
            'publish_date':     meta.get('publish_date', ''),
            'preview_path':     preview_rel,
            'preview_text':     meta.get('preview', get_text_preview(full_path)),
            'file_data':        engine.get_metadata(full_path),
        }

    # ── Flask routes ──────────────────────────────────────────────────────

    @app.route('/youtube_files/<path:filename>')
    def serve_youtube_files(filename):
        return send_from_directory(storage_dir, filename)

    @app.route('/youtube_stream/<youtube_id>')
    def stream_youtube_video(youtube_id):
        """Proxy a pre-muxed YouTube stream with Range support (seekable).
        ?quality=1080  — one of 360/480/720/1080/1440/2160/best (default 1080)
        ?info=1        — JSON-only: returns {ok, height}
        """
        quality = flask_request.args.get('quality', '1080')
        result = _get_or_fetch_urls(storage_dir, youtube_id, quality)
        if 'error' in result:
            return jsonify(result), (403 if result.get('needs_cookies') else 500)
        if flask_request.args.get('info') == '1':
            return jsonify({'ok': True, 'height': result.get('height')})
        target = result.get('url')
        if not target:
            return jsonify({'error': 'No stream URL available'}), 500
        return _proxy_cdn(target)

    # ── Socket handlers ───────────────────────────────────────────────────

    @socketio.on('emit_youtube_page_add_video')
    def handle_add_video(data):
        url = (data.get('url', '') or '').strip()
        if not url:
            return {'error': 'No URL provided'}
        user_rating = data.get('user_rating', None)

        def _task(ctx):
            ctx.update(0.1, 'Fetching video info…')
            ctx.check()
            video_info = fetch_video_info(url)
            if video_info is None:
                socketio.emit('emit_youtube_page_video_added', {'error': f'Could not fetch: {url}'})
                return

            ctx.update(0.4, 'Storing video…')
            ctx.check()
            result = store_video(storage_dir, video_info,
                                 subfolder=tasks.resolve_subfolder(storage_dir, video_info))
            if 'error' in result:
                socketio.emit('emit_youtube_page_video_added', result)
                return

            ctx.update(0.7, 'Updating database…')
            ctx.check()
            video_id = tasks.upsert_video_db(app, result, video_info, user_rating)

            ctx.update(0.85, 'Scoring video…')
            tasks.score_and_update(video_id, deps)

            with app.app_context():
                row = db_models.YoutubeLibrary.query.get(video_id)
                socketio.emit('emit_youtube_page_video_added', row.as_dict() if row else {})
            ctx.update(1.0, 'Done')

        task_manager.submit(f'Add YouTube video: {url}', _task)

    @socketio.on('emit_youtube_page_add_channel')
    def handle_add_channel(data):
        channel_url = (data.get('url', '') or '').strip()
        if not channel_url:
            return {'error': 'No channel URL provided'}
        user_rating = data.get('user_rating', None)

        def _task(ctx):
            def _progress(msg):
                ctx.check()
                socketio.emit('emit_youtube_page_import_progress', {'message': msg})

            ctx.update(0.05, 'Listing channel videos…')
            _progress('Listing channel videos…')
            video_list = fetch_channel_videos(channel_url, status_callback=_progress)
            if not video_list:
                _progress('No videos found on this channel.')
                return

            _progress(f'Found {len(video_list)} videos. Importing…')
            added = tasks.import_video_list(ctx, deps, video_list,
                                            user_rating=user_rating, auto_update=True,
                                            on_progress=_progress)
            _progress(f'Channel import complete — {len(video_list)} videos processed.')
            ctx.update(1.0, 'Done')
            socketio.emit('emit_youtube_page_channel_imported', {'count': added})

        task_manager.submit(f'Import YouTube channel: {channel_url}', _task)

    @socketio.on('emit_youtube_page_add_playlist')
    def handle_add_playlist(data):
        playlist_url = (data.get('url', '') or '').strip()
        if not playlist_url:
            return {'error': 'No playlist URL provided'}
        user_rating = data.get('user_rating', None)

        def _task(ctx):
            def _progress(msg):
                ctx.check()
                socketio.emit('emit_youtube_page_import_progress', {'message': msg})

            ctx.update(0.05, 'Listing playlist videos…')
            _progress('Listing playlist videos…')
            video_list = fetch_playlist_videos(playlist_url, status_callback=_progress)
            if not video_list:
                _progress('No videos found in this playlist.')
                return

            _progress(f'Found {len(video_list)} videos. Importing…')
            added = tasks.import_video_list(ctx, deps, video_list,
                                            user_rating=user_rating, auto_update=False,
                                            on_progress=_progress)
            _progress(f'Playlist import complete — {len(video_list)} videos processed.')
            ctx.update(1.0, 'Done')
            socketio.emit('emit_youtube_page_playlist_imported', {'count': added})

        task_manager.submit(f'Import YouTube playlist: {playlist_url}', _task)

    @socketio.on('emit_youtube_page_upload_cookies')
    def handle_upload_cookies(data):
        """Save an uploaded cookies.txt file to the YouTube storage directory."""
        content = (data or {}).get('content', '')
        if not content or not content.strip():
            return {'ok': False, 'error': 'Empty file'}
        # Basic Netscape cookies.txt sanity check
        stripped = content.lstrip()
        if not (stripped.startswith('# Netscape HTTP Cookie File') or
                stripped.startswith('# HTTP Cookie File') or
                '\t' in stripped.split('\n')[0] if stripped else False):
            # Be lenient — just check it has tab-separated lines
            pass
        cookies_path = os.path.join(storage_dir, 'cookies.txt')
        try:
            os.makedirs(storage_dir, exist_ok=True)
            with open(cookies_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f'[YouTube] cookies.txt saved to {cookies_path}')
            # Clear the stream URL cache so new requests use the cookies
            with _stream_url_lock:
                _stream_url_cache.clear()
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    @socketio.on('emit_youtube_page_get_stream_url')
    def handle_get_stream_url(data):
        youtube_id = (data.get('youtube_id', '') or '').strip()
        quality    = (data.get('quality', '1080') or '1080').strip()
        if not youtube_id:
            return {'error': 'No youtube_id provided'}
        result = _get_or_fetch_urls(storage_dir, youtube_id, quality)
        if 'error' in result:
            return result
        video = db_models.YoutubeLibrary.query.filter_by(url=youtube_id).first()
        if video:
            tasks.touch_last_viewed(video)
        return {'url': f'/youtube_stream/{youtube_id}?quality={quality}'}

    @socketio.on('emit_youtube_page_get_folders')
    def handle_get_folders(data):
        path = data.get('path', '') if data else ''
        result = yt_file_manager.get_folders(path)
        if result and result.get('folders'):
            _sort_folder_tree(result['folders'])
        return result
        if result and result.get('folders'):
            _sort_folder_tree(result['folders'])
        return result

    @socketio.on('emit_youtube_page_get_files')
    def handle_get_files(input_data):
        _allowed = {'path', 'pagination', 'limit', 'text_query', 'seed',
                    'filters', 'get_file_info', 'update_model_ratings',
                    'mode', 'order', 'temperature', 'evaluator_hash'}
        params = {k: v for k, v in input_data.items() if k in _allowed}
        params.update({
            'filters':              filters,
            'get_file_info':        _get_file_info,
            'update_model_ratings': lambda fps: tasks.update_model_ratings(fps, deps),
            'evaluator_hash':       evaluator.hash,
        })
        return yt_file_manager.get_files(**params)

    @socketio.on('emit_youtube_page_set_rating')
    def handle_set_rating(data):
        file_hash = data.get('hash')
        file_path = data.get('file_path')
        rating = data.get('rating')
        if rating is None:
            return {'error': 'rating required'}

        video_id = os.path.basename(file_path or '').replace('.link', '') if file_path else ''
        video = None
        if video_id:
            video = db_models.YoutubeLibrary.query.filter_by(url=video_id).first()
        if video is None and file_path:
            video = db_models.YoutubeLibrary.query.filter_by(file_path=file_path).first()
        if video is None:
            video = db_models.YoutubeLibrary(
                hash=file_hash,
                hash_algorithm=HASH_ALGORITHM,
                file_path=file_path,
                url=video_id or None,
            )
            db_models.db.session.add(video)

        if video is None:
            return {'error': 'Could not find or create video record'}

        video.user_rating = float(rating)
        video.user_rating_date = datetime.datetime.utcnow()
        tasks.touch_last_viewed(video)
        return video.as_dict()

    @socketio.on('emit_youtube_page_mark_viewed')
    def handle_mark_viewed(data):
        youtube_id = (data or {}).get('youtube_id', '')
        if youtube_id:
            video = db_models.YoutubeLibrary.query.filter_by(url=youtube_id).first()
            if video:
                tasks.touch_last_viewed(video)

    @socketio.on('emit_youtube_page_read_channel_yaml')
    def handle_read_channel_yaml(data):
        folder_path = (data.get('path', '') or '').strip()
        abs_folder = (
            os.path.abspath(os.path.join(storage_dir, '..', folder_path))
            if folder_path else storage_dir
        )
        conf = read_channel_yaml(abs_folder)
        if conf:
            return conf
        # Scan sub-directories for channels
        channels = []
        try:
            for entry in sorted(os.scandir(abs_folder), key=lambda e: e.name):
                if entry.is_dir() and read_channel_yaml(entry.path):
                    channels.append(entry.name)
        except OSError:
            pass
        if channels:
            return {'mode': 'list', 'channels': channels}
        return {}

    # ── .meta file handlers ──────────────────────────────────────────────
    class _MetaReader:
        def generate_full_description(self, full_path, media_folder):
            return read_video_text(full_path)

    register_meta_handlers(socketio, 'YouTube', lambda: storage_dir, _MetaReader())

    # ── Startup tasks ─────────────────────────────────────────────────────
    task_manager.submit('YouTube: convert PNG previews to JPEG',
                        lambda ctx: tasks.task_migrate_png_previews(ctx, deps))
    if tasks.find_folders_to_migrate(storage_dir):
        task_manager.submit('YouTube: migrate channel folder names',
                            lambda ctx: tasks.task_migrate_channel_folders(ctx, deps))

    # ── Scheduled tasks ───────────────────────────────────────────────────
    schedule_task(app, score_interval,
                  lambda: task_manager.submit(
                      'YouTube: score unscored videos',
                      lambda ctx: tasks.task_score_videos(ctx, deps)), start_immediately=True)

    # ── Scheduled channel sync ───────────────────────────────────────────
    channel_update_interval = OmegaConf.select(
        cfg, 'YouTube.channel_update_interval_minutes', default=30)
    channel_update_recent_count = int(OmegaConf.select(
        cfg, 'YouTube.channel_update_recent_count', default=15))

    schedule_task(app, channel_update_interval,
                  lambda: task_manager.submit(
                      'YouTube: sync channel updates',
                      lambda ctx: tasks.task_sync_channels(ctx, deps)))

    schedule_task(app, transcript_backfill_interval,
                  lambda: task_manager.submit(
                      'YouTube: backfill transcripts',
                      lambda ctx: tasks.task_backfill_transcripts(ctx, deps)))

    common_socket_events.show_loading_status('YouTube module ready!')
