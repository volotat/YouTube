"""
YouTube module – serve.py

Local YouTube browsing: videos stored as .link files with YAML front-matter,
thumbnails as .link.preview.png, rich metadata in .link.meta.  Videos stream
via yt-dlp extracted direct URLs in a native <video> player (no YouTube UI).

Architecture mirrors WebSearch (frontmatter, lean DB, custom engine adapter)
with Videos module presentation (thumbnails, modal player, FileGridComponent).
"""

import os
import datetime
import time
import threading
import urllib.request
import urllib.error

import torch
from flask import Flask, send_from_directory, Response, request as flask_request, jsonify
from flask_socketio import SocketIO
from omegaconf import OmegaConf
import numpy as np
import rapidfuzz.fuzz

import modules.YouTube.db_models as db_models
from modules.YouTube.fetcher import (
    HASH_ALGORITHM, parse_frontmatter, build_frontmatter,
    _blake2b, fetch_video_info, fetch_channel_videos, fetch_channel_recent_videos,
    fetch_playlist_videos, extract_stream_url, get_stream_urls, store_video,
    find_channel_folder, write_channel_yaml, read_channel_yaml, _safe_name,
)
from src.socket_events import CommonSocketEvents
from src.text_embedder import TextEmbedder
from modules.train.universal_train import UniversalEvaluator
import src.file_manager as file_manager
from src.caching import TwoLevelCache
from src.common_filters import CommonFilters, _normalize_text
from src.recommendation_engine import sort_files_by_recommendation
from src.module_helpers import register_meta_handlers, make_scheduled_rating_check
from src.scheduler import schedule_task


# ── _YoutubeTextEngine ───────────────────────────────────────────────────
# Minimal engine adapter that gives CommonFilters / FileManager the
# interface they need.  Hash is computed from .link file bytes.

class _YoutubeTextEngine:
    def __init__(self, text_embedder, cache, storage_dir, cfg=None):
        self._emb = text_embedder
        self._cache = cache
        self.storage_dir = storage_dir
        self.cfg = cfg
        self._emb_dim_cache = None

    # ── FileManager / CommonFilters interface ────────────────────────

    def get_file_hash(self, path: str) -> str:
        if not os.path.exists(path):
            return ''
        st = os.stat(path, follow_symlinks=False)
        cache_key = f"HASH_OF_FILE::{path}::{st.st_size}::{st.st_mtime_ns}::{HASH_ALGORITHM}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        with open(path, 'rb') as f:
            file_hash = _blake2b(f.read())
        self._cache.set(cache_key, file_hash)
        return file_hash

    def get_hash_algorithm(self) -> str:
        return HASH_ALGORITHM

    def get_metadata(self, file_path: str) -> dict:
        meta = {}
        try:
            meta['file_size'] = os.path.getsize(file_path)
            meta['creation_time'] = os.path.getctime(file_path)
            meta['modification_time'] = os.path.getmtime(file_path)
        except Exception:
            meta['file_size'] = None
            meta['creation_time'] = None
            meta['modification_time'] = None
        return meta

    def _get_media_folder(self) -> str:
        return self.storage_dir

    # ── Front-matter reading (cached) ────────────────────────────────

    def get_frontmatter(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        st = os.stat(path, follow_symlinks=False)
        cache_key = f"FRONTMATTER::{path}::{st.st_size}::{st.st_mtime_ns}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            with open(path, 'r', encoding='utf-8') as f:
                meta, _ = parse_frontmatter(f.read())
        except Exception:
            meta = {}
        self._cache.set(cache_key, meta)
        return meta

    def get_title_and_url(self, path: str) -> tuple[str, str]:
        meta = self.get_frontmatter(path)
        return (meta.get('title', os.path.basename(path)),
                meta.get('url', ''))

    # ── Embedding interface (for semantic search) ────────────────────

    def process_text(self, text: str):
        return np.array(self._emb.embed_text(text))

    def _emb_dim(self) -> int:
        if self._emb_dim_cache is None:
            try:
                self._emb_dim_cache = self._emb.embedding_dim or 1024
            except Exception:
                self._emb_dim_cache = 1024
        return self._emb_dim_cache

    def process_files(self, file_paths, callback=None, media_folder=None):
        rows = []
        for path in file_paths:
            file_hash = self.get_file_hash(path)
            cache_key = f'emb:{file_hash or path}'
            cached = self._cache.get(cache_key)
            if cached is not None:
                rows.append(cached)
                continue
            try:
                # Prefer .meta file for richer embedding, fall back to frontmatter
                meta_path = path + '.meta'
                if os.path.exists(meta_path):
                    with open(meta_path, 'r', encoding='utf-8') as fh:
                        body = fh.read()
                else:
                    with open(path, 'r', encoding='utf-8') as fh:
                        _, body = parse_frontmatter(fh.read())
                emb = self._emb.embed_text(body)
                vec = np.array(emb).mean(axis=0) if emb is not None and len(emb) else np.zeros(self._emb_dim())
            except Exception:
                vec = np.zeros(self._emb_dim())
            self._cache.set(cache_key, vec)
            rows.append(vec)
        return torch.tensor(np.stack(rows), dtype=torch.float32) if rows else torch.zeros((0, self._emb_dim()))

    def process_query(self, text: str):
        """Embed a query string. Alias of process_text; satisfies the metadata_engine interface."""
        return self.process_text(text)

    def compare(self, embeds_files, embeds_text):
        ef = embeds_files.numpy() if isinstance(embeds_files, torch.Tensor) else np.array(embeds_files)
        qt = np.array(embeds_text)
        if qt.ndim > 1:
            qt = qt.mean(axis=0)
        norms = np.linalg.norm(ef, axis=1) * np.linalg.norm(qt)
        return np.dot(ef, qt) / np.maximum(norms, 1e-8)


# ── Module-level helpers ─────────────────────────────────────────────────

_stream_url_cache: dict = {}
_stream_url_lock = threading.Lock()

def _get_text_preview(file_path: str) -> str:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            meta, body = parse_frontmatter(f.read())
        preview = meta.get('preview', '')
        if preview:
            return preview
        text = body[:300].strip()
        if len(body) > 300:
            text += '…'
        return text
    except Exception:
        return ''


def init_socket_events(socketio: SocketIO, app: Flask = None, cfg=None, data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="YouTube")

    # ── Storage directory ────────────────────────────────────────────────
    storage_dir = OmegaConf.select(cfg, "YouTube.storage_directory",
                                   default="/mnt/project_config/modules/YouTube")
    os.makedirs(storage_dir, exist_ok=True)

    # ── Cache ────────────────────────────────────────────────────────────
    yt_cache = TwoLevelCache(
        cache_dir=os.path.join(cfg.main.cache_path, 'YouTube'),
        name='youtube_embeddings',
    )

    # ── Text embedder ────────────────────────────────────────────────────
    common_socket_events.show_loading_status('Initializing text embedder for YouTube…')
    text_embedder = TextEmbedder(cfg=cfg)
    text_embedder.initiate(models_folder=cfg.main.embedding_models_path)

    yt_engine = _YoutubeTextEngine(text_embedder, yt_cache, storage_dir, cfg=cfg)

    # ── FileManager ──────────────────────────────────────────────────────
    common_socket_events.show_loading_status('Setting up YouTube file manager…')
    yt_file_manager = file_manager.FileManager(
        cfg=cfg,
        media_directory=storage_dir,
        engine=yt_engine,
        module_name="YouTube",
        media_formats={'.link'},
        socketio=socketio,
        db_schema=db_models.YoutubeLibrary,
    )

    yt_filters = CommonFilters(
        engine=yt_engine,
        metadata_engine=yt_engine,
        common_socket_events=common_socket_events,
        media_directory=storage_dir,
        db_schema=db_models.YoutubeLibrary,
        update_model_ratings_func=None,  # set below
    )

    # ── Universal evaluator ──────────────────────────────────────────────
    common_socket_events.show_loading_status('Loading universal evaluator for YouTube…')
    evaluator = UniversalEvaluator()
    evaluator_path = os.path.join(cfg.main.personal_models_path, 'universal_evaluator.pt')
    if os.path.exists(evaluator_path):
        evaluator.load(evaluator_path)
    else:
        print("[YouTube] universal_evaluator.pt not found – model scoring disabled.")

    # ── Scoring state ────────────────────────────────────────────────────
    _scoring_state = {'last_hash': None, 'in_progress': False}

    def _score_video(file_path: str):
        full_path = os.path.join(storage_dir, file_path) if not os.path.isabs(file_path) else file_path
        if not os.path.exists(full_path):
            return None
        try:
            # Prefer .meta for richer text
            meta_path = full_path + '.meta'
            if os.path.exists(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    body = f.read()
            else:
                with open(full_path, 'r', encoding='utf-8') as f:
                    _, body = parse_frontmatter(f.read())
            if not body or len(body.strip()) < 10:
                return None
            chunk_embeddings = text_embedder.embed_text(body)
            ratings = evaluator.predict([chunk_embeddings])
            return float(ratings[0])
        except Exception as exc:
            print(f"[YouTube] scoring error for {file_path}: {exc}")
            return None

    def _score_and_update(video_id: int):
        with app.app_context():
            video = db_models.YoutubeLibrary.query.get(video_id)
            if video is None or video.file_path is None:
                return
            if video.model_rating is not None and video.model_hash == evaluator.hash:
                return
            rating = _score_video(video.file_path)
            if rating is not None:
                video.model_rating = rating
                video.model_hash = evaluator.hash
                db_models.db.session.commit()

    def _task_score_videos(ctx):
        current_hash = evaluator.hash
        try:
            videos = db_models.YoutubeLibrary.query.filter(
                (db_models.YoutubeLibrary.model_rating.is_(None)) |
                (db_models.YoutubeLibrary.model_hash != current_hash)
            ).all()
            total = len(videos)
            if total == 0:
                _scoring_state['last_hash'] = current_hash
                return
            print(f"[YouTube] Re-scoring {total} videos with evaluator {current_hash}…")
            for i, video in enumerate(videos):
                ctx.check()
                ctx.update((i + 1) / total, f"Scoring video {i + 1}/{total}…")
                if video.file_path is None:
                    continue
                rating = _score_video(video.file_path)
                if rating is not None:
                    video.model_rating = rating
                    video.model_hash = current_hash
            db_models.db.session.commit()
            print(f"[YouTube] Scoring complete ({total} videos).")
            _scoring_state['last_hash'] = current_hash
        finally:
            _scoring_state['in_progress'] = False

    def _maybe_trigger_rescore():
        if _scoring_state['in_progress']:
            return
        if evaluator.hash is None:
            return
        if _scoring_state['last_hash'] != evaluator.hash:
            _scoring_state['in_progress'] = True
            task_manager.submit('YouTube: score unscored videos', _task_score_videos)

    def _update_model_ratings(file_paths):
        for abs_path in file_paths:
            file_hash = yt_engine.get_file_hash(abs_path)
            if not file_hash:
                continue
            video = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first()
            if video:
                _score_and_update(video.id)

    # Patch into filters
    yt_filters.update_model_ratings_func = _update_model_ratings

    def _touch_last_viewed(video):
        video.last_viewed = datetime.datetime.utcnow()
        db_models.db.session.commit()

    # ── Task manager ─────────────────────────────────────────────────────
    task_manager = app.task_manager

    # ── Flask route to serve preview images + .link files ────────────────
    @app.route('/youtube_files/<path:filename>')
    def serve_youtube_files(filename):
        return send_from_directory(storage_dir, filename)

    # ── Stream proxy helpers ──────────────────────────────────────────────

    def _get_or_fetch_urls(youtube_id, quality):
        """Return cached yt-dlp stream URLs or fetch fresh ones (cached ~4 h)."""
        cache_key = f"{youtube_id}:{quality}"
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

    def _proxy_cdn(target_url):
        """Proxy a YouTube CDN URL forwarding Range headers for seekability."""
        headers = {'User-Agent': 'Mozilla/5.0'}
        range_hdr = flask_request.headers.get('Range')
        if range_hdr:
            headers['Range'] = range_hdr
        req = urllib.request.Request(target_url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            return Response(exc.read(), status=exc.code,
                            content_type='text/plain')
        resp_headers = {
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
        }
        ct = resp.headers.get('Content-Type', 'video/mp4')
        resp_headers['Content-Type'] = ct
        cl = resp.headers.get('Content-Length')
        if cl:
            resp_headers['Content-Length'] = cl
        cr = resp.headers.get('Content-Range')
        if cr:
            resp_headers['Content-Range'] = cr
        status = 206 if cr else 200
        def _generate():
            try:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()
        return Response(_generate(), status=status, headers=resp_headers)

    @app.route('/youtube_stream/<youtube_id>')
    def stream_youtube_video(youtube_id):
        """Proxy a pre-muxed YouTube stream with Range support (seekable).
        ?quality=1080  — one of 360/480/720/1080/1440/2160/best (default 1080)
        ?info=1        — JSON-only: returns {ok, height}
        """
        quality = flask_request.args.get('quality', '1080')
        result = _get_or_fetch_urls(youtube_id, quality)
        if 'error' in result:
            code = 403 if result.get('needs_cookies') else 500
            return jsonify(result), code
        if flask_request.args.get('info') == '1':
            return jsonify({'ok': True, 'height': result.get('height')})
        target = result.get('url')
        if not target:
            return jsonify({'error': 'No stream URL available'}), 500
        return _proxy_cdn(target)

    # ── Socket handlers ──────────────────────────────────────────────────

    @socketio.on('emit_youtube_page_add_video')
    def handle_add_video(data):
        url = (data.get('url', '') or '').strip()
        if not url:
            return {'error': 'No URL provided'}
        user_rating = data.get('user_rating', None)

        def _task_add(ctx):
            ctx.update(0.1, f'Fetching video info…')
            ctx.check()
            video_info = fetch_video_info(url)
            if video_info is None:
                socketio.emit('emit_youtube_page_video_added', {'error': f'Could not fetch: {url}'})
                return

            ctx.update(0.4, 'Storing video…')
            ctx.check()

            # Find existing channel folder or use channel name
            subfolder = None
            if video_info.get('channel_id'):
                subfolder = find_channel_folder(storage_dir, video_info['channel_id'])
            if not subfolder:
                subfolder = _safe_name(video_info.get('author', '') or 'uncategorized')

            result = store_video(storage_dir, video_info, subfolder=subfolder)
            if 'error' in result:
                socketio.emit('emit_youtube_page_video_added', result)
                return

            ctx.update(0.7, 'Updating database…')
            ctx.check()

            # Create or update DB row
            with app.app_context():
                existing = db_models.YoutubeLibrary.query.filter_by(hash=result['hash']).first()
                if not existing:
                    existing = db_models.YoutubeLibrary(
                        hash=result['hash'],
                        hash_algorithm=HASH_ALGORITHM,
                        file_path=result['file_path'],
                        url=video_info.get('youtube_id', ''),
                    )
                    db_models.db.session.add(existing)
                else:
                    existing.file_path = result['file_path']
                if user_rating is not None:
                    existing.user_rating = float(user_rating)
                    existing.user_rating_date = datetime.datetime.utcnow()
                db_models.db.session.commit()

                # Score
                ctx.update(0.85, 'Scoring video…')
                _score_and_update(existing.id)

                socketio.emit('emit_youtube_page_video_added', existing.as_dict())
            ctx.update(1.0, 'Done')

        task_manager.submit(f'Add YouTube video: {url}', _task_add)

    @socketio.on('emit_youtube_page_add_channel')
    def handle_add_channel(data):
        channel_url = (data.get('url', '') or '').strip()
        if not channel_url:
            return {'error': 'No channel URL provided'}
        user_rating = data.get('user_rating', None)

        def _task_add_channel(ctx):
            def _status(msg):
                ctx.check()
                socketio.emit('emit_youtube_page_import_progress', {'message': msg})

            ctx.update(0.05, 'Listing channel videos…')
            _status('Listing channel videos…')

            video_list = fetch_channel_videos(channel_url, status_callback=_status)
            if not video_list:
                _status('No videos found on this channel.')
                return

            total = len(video_list)
            _status(f'Found {total} videos. Importing…')

            for i, entry in enumerate(video_list):
                ctx.check()
                progress = 0.1 + 0.85 * (i / max(total, 1))
                ctx.update(progress, f'Importing {i + 1}/{total}: {entry.get("title", "")[:50]}…')
                _status(f'Importing {i + 1}/{total}…')

                vid_url = entry.get('url', '')
                if not vid_url:
                    continue

                # Check if already stored
                vid_id = entry.get('youtube_id', '')
                if vid_id:
                    link_files = []
                    for root, dirs, files in os.walk(storage_dir):
                        for fn in files:
                            if fn == f'{vid_id}.link':
                                link_files.append(os.path.join(root, fn))
                    if link_files:
                        continue

                video_info = fetch_video_info(vid_url)
                if video_info is None:
                    continue

                # Determine subfolder
                subfolder = None
                if video_info.get('channel_id'):
                    subfolder = find_channel_folder(storage_dir, video_info['channel_id'])
                if not subfolder:
                    subfolder = _safe_name(video_info.get('author', '') or 'uncategorized')

                result = store_video(storage_dir, video_info, subfolder=subfolder)
                if 'error' in result:
                    continue

                with app.app_context():
                    existing = db_models.YoutubeLibrary.query.filter_by(hash=result['hash']).first()
                    if not existing:
                        existing = db_models.YoutubeLibrary(
                            hash=result['hash'],
                            hash_algorithm=HASH_ALGORITHM,
                            file_path=result['file_path'],
                            url=video_info.get('youtube_id', ''),
                        )
                        db_models.db.session.add(existing)
                    if user_rating is not None:
                        existing.user_rating = float(user_rating)
                        existing.user_rating_date = datetime.datetime.utcnow()
                    db_models.db.session.commit()

            _status(f'Channel import complete — {total} videos processed.')
            ctx.update(1.0, 'Done')
            socketio.emit('emit_youtube_page_channel_imported', {'count': total})

        task_manager.submit(f'Import YouTube channel: {channel_url}', _task_add_channel)

    @socketio.on('emit_youtube_page_add_playlist')
    def handle_add_playlist(data):
        playlist_url = (data.get('url', '') or '').strip()
        if not playlist_url:
            return {'error': 'No playlist URL provided'}
        user_rating = data.get('user_rating', None)

        def _task_add_playlist(ctx):
            def _status(msg):
                ctx.check()
                socketio.emit('emit_youtube_page_import_progress', {'message': msg})

            ctx.update(0.05, 'Listing playlist videos…')
            _status('Listing playlist videos…')

            video_list = fetch_playlist_videos(playlist_url, status_callback=_status)
            if not video_list:
                _status('No videos found in this playlist.')
                return

            total = len(video_list)
            _status(f'Found {total} videos. Importing…')

            for i, entry in enumerate(video_list):
                ctx.check()
                progress = 0.1 + 0.85 * (i / max(total, 1))
                ctx.update(progress, f'Importing {i + 1}/{total}: {entry.get("title", "")[:50]}…')
                _status(f'Importing {i + 1}/{total}…')

                vid_url = entry.get('url', '')
                if not vid_url:
                    continue

                # Skip already-stored videos
                vid_id = entry.get('youtube_id', '')
                if vid_id:
                    link_files = []
                    for root, dirs, files in os.walk(storage_dir):
                        for fn in files:
                            if fn == f'{vid_id}.link':
                                link_files.append(os.path.join(root, fn))
                    if link_files:
                        continue

                video_info = fetch_video_info(vid_url)
                if video_info is None:
                    continue

                subfolder = None
                if video_info.get('channel_id'):
                    subfolder = find_channel_folder(storage_dir, video_info['channel_id'])
                if not subfolder:
                    subfolder = _safe_name(video_info.get('author', '') or 'uncategorized')

                result = store_video(storage_dir, video_info, subfolder=subfolder)
                if 'error' in result:
                    continue

                with app.app_context():
                    existing = db_models.YoutubeLibrary.query.filter_by(hash=result['hash']).first()
                    if not existing:
                        existing = db_models.YoutubeLibrary(
                            hash=result['hash'],
                            hash_algorithm=HASH_ALGORITHM,
                            file_path=result['file_path'],
                            url=video_info.get('youtube_id', ''),
                        )
                        db_models.db.session.add(existing)
                    if user_rating is not None:
                        existing.user_rating = float(user_rating)
                        existing.user_rating_date = datetime.datetime.utcnow()
                    db_models.db.session.commit()

            _status(f'Playlist import complete — {total} videos processed.')
            ctx.update(1.0, 'Done')
            socketio.emit('emit_youtube_page_playlist_imported', {'count': total})

        task_manager.submit(f'Import YouTube playlist: {playlist_url}', _task_add_playlist)

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
        quality = (data.get('quality', '1080') or '1080').strip()
        if not youtube_id:
            return {'error': 'No youtube_id provided'}
        # Pre-fetch into cache so the first HTTP Range request is fast
        result = _get_or_fetch_urls(youtube_id, quality)
        if 'error' in result:
            return result
        # Update last_viewed
        file_hash = data.get('hash', '')
        if file_hash:
            video = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first()
            if video:
                _touch_last_viewed(video)
        return {'url': f'/youtube_stream/{youtube_id}?quality={quality}'}

    @socketio.on('emit_youtube_page_get_folders')
    def handle_get_folders(data):
        path = data.get('path', '') if data else ''
        return yt_file_manager.get_folders(path)

    @socketio.on('emit_youtube_page_get_files')
    def handle_get_files(input_data):
        # ── Filters ──────────────────────────────────────────────────────
        def _filter_fuzzy_title(files, text_query, **__):
            q = _normalize_text(text_query)
            q_raw = text_query.strip().lower()
            scorer = rapidfuzz.fuzz.token_set_ratio if ' ' in q else rapidfuzz.fuzz.WRatio
            scores = []
            for f in files:
                title, url = yt_engine.get_title_and_url(f)
                s_title = scorer(q, _normalize_text(title))
                s_url = scorer(q, _normalize_text(url))
                combined = max(1.3 * s_title, s_url)
                priority = 0
                if q_raw and q_raw in title.lower():
                    priority = 3
                elif q_raw and q_raw in url.lower():
                    priority = 2
                scores.append(priority * 10.0 + combined)
            return np.array(scores, dtype=np.float32) / 100.0

        def _filter_by_text(files, text_query, mode='file-name', **kw):
            if mode == 'file-name':
                return _filter_fuzzy_title(files, text_query)
            return yt_filters.filter_by_text(files, text_query, mode=mode, **kw)

        def _filter_recommendation(files, *_, **__):
            files_data = []
            for f in files:
                file_hash = yt_engine.get_file_hash(f)
                db_item = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first() if file_hash else None
                files_data.append({
                    'user_rating': db_item.user_rating if db_item else None,
                    'model_rating': db_item.model_rating if db_item else None,
                    'full_play_count': 1,
                    'skip_count': 0,
                    'last_played': db_item.last_viewed if db_item else None,
                })
            return np.array(sort_files_by_recommendation(files, files_data), dtype=np.float32)

        def _filter_recent(files, *_, **__):
            ts = []
            for f in files:
                meta = yt_engine.get_frontmatter(f)
                try:
                    dt = datetime.datetime.fromisoformat(str(meta.get('publish_date', '')))
                    ts.append(dt.timestamp())
                except Exception:
                    ts.append(0.0)
            ts = np.array(ts, dtype=np.float32)
            rng = ts.max() - ts.min() if len(ts) else 0
            return (ts - ts.min()) / (rng + 1e-8)

        filters = {
            'by_text': _filter_by_text,
            'rating': yt_filters.filter_by_rating,
            'random': yt_filters.filter_by_random, 
            'recommendation': _filter_recommendation,
            'recent': _filter_recent,
        }

        def get_file_info(full_path, file_hash):
            db_item = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first()
            meta = yt_engine.get_frontmatter(full_path)

            # Preview image path (relative to storage_dir)
            preview_rel = None
            preview_abs = full_path + '.preview.png'
            if os.path.exists(preview_abs):
                preview_rel = os.path.relpath(preview_abs, storage_dir)

            return {
                'user_rating': db_item.user_rating if db_item else None,
                'model_rating': db_item.model_rating if db_item else None,
                'last_viewed': db_item.last_viewed.isoformat() if db_item and db_item.last_viewed else None,
                'url': meta.get('url', ''),
                'youtube_id': meta.get('youtube_id', ''),
                'title': meta.get('title', ''),
                'author': meta.get('author', ''),
                'duration': meta.get('duration', ''),
                'duration_seconds': meta.get('duration_seconds', 0),
                'publish_date': meta.get('publish_date', ''),
                'preview_path': preview_rel,
                'preview_text': meta.get('preview', _get_text_preview(full_path)),
                'file_data': yt_engine.get_metadata(full_path),
            }

        def update_model_ratings(file_paths):
            _update_model_ratings(file_paths)

        _allowed = {
            'path', 'pagination', 'limit', 'text_query', 'seed', 'filters',
            'get_file_info', 'update_model_ratings', 'mode', 'order',
            'temperature', 'evaluator_hash',
        }
        input_params = {k: v for k, v in input_data.items() if k in _allowed}
        input_params.update({
            'filters': filters,
            'get_file_info': get_file_info,
            'update_model_ratings': update_model_ratings,
            'evaluator_hash': evaluator.hash,
        })

        _maybe_trigger_rescore()
        return yt_file_manager.get_files(**input_params)

    @socketio.on('emit_youtube_page_set_rating')
    def handle_set_rating(data):
        file_hash = data.get('hash')
        file_path = data.get('file_path')
        rating = data.get('rating')
        if rating is None:
            return {'error': 'rating required'}

        video = None
        if file_hash:
            video = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first()
        if video is None and file_path:
            video = db_models.YoutubeLibrary.query.filter_by(file_path=file_path).first()
        if video is None and file_hash:
            video = db_models.YoutubeLibrary(
                hash=file_hash,
                hash_algorithm=HASH_ALGORITHM,
                file_path=file_path,
            )
            db_models.db.session.add(video)

        if video is None:
            return {'error': 'Could not find or create video record'}

        video.user_rating = float(rating)
        video.user_rating_date = datetime.datetime.utcnow()
        _touch_last_viewed(video)
        return video.as_dict()

    @socketio.on('emit_youtube_page_mark_viewed')
    def handle_mark_viewed(data):
        file_hash = data.get('hash', '')
        if file_hash:
            video = db_models.YoutubeLibrary.query.filter_by(hash=file_hash).first()
            if video:
                _touch_last_viewed(video)

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
    # Simple adapter so get_full_metadata_description reads the .meta file.
    class _MetaReader:
        def generate_full_description(self, full_path, media_folder):
            meta_path = full_path + '.meta'
            if os.path.exists(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    return f.read()
            return ''

    register_meta_handlers(socketio, 'YouTube', lambda: storage_dir, _MetaReader())

    # ── Startup background tasks ─────────────────────────────────────────
    if os.path.exists(evaluator_path):
        _scoring_state['in_progress'] = True
        task_manager.submit('YouTube: score unscored videos', _task_score_videos)

    # ── Scheduled channel sync ───────────────────────────────────────────
    channel_update_interval = OmegaConf.select(
        cfg, 'YouTube.channel_update_interval_minutes', default=30)
    channel_update_recent_count = int(OmegaConf.select(
        cfg, 'YouTube.channel_update_recent_count', default=15))

    def _task_sync_channels(ctx):
        """Check every tracked channel for videos newer than what is already stored.

        Cost model: 1 flat-extraction API call per channel + 1 full-metadata
        call only for each genuinely new video.  All already-stored videos are
        detected with a fast filesystem check (no network round-trip).
        """
        channel_dirs = []
        try:
            for entry in os.scandir(storage_dir):
                if not entry.is_dir():
                    continue
                conf = read_channel_yaml(entry.path)
                if conf and conf.get('channel_url'):
                    channel_dirs.append((entry.name, entry.path, conf))
        except OSError as exc:
            print(f'[YouTube] channel sync scan error: {exc}')
            return

        if not channel_dirs:
            return

        total = len(channel_dirs)
        added_total = 0
        print(f'[YouTube] Channel sync started — {total} channel(s) tracked.')

        for i, (folder_name, folder_path, conf) in enumerate(channel_dirs):
            ctx.check()
            channel_name = conf.get('channel_name') or folder_name
            ctx.update(i / total, f'Syncing {i + 1}/{total}: {channel_name}')

            channel_url = conf.get('channel_url', '')
            channel_id = conf.get('channel_id', '')
            if not channel_url:
                continue

            # One API call: fetch the N most-recent video stubs for this channel.
            recent = fetch_channel_recent_videos(channel_url, count=channel_update_recent_count)
            added = 0

            for video_entry in recent:
                ctx.check()
                vid_id = video_entry.get('youtube_id', '')
                if not vid_id:
                    continue

                # Fast filesystem check — no network call if already stored.
                if os.path.exists(os.path.join(folder_path, f'{vid_id}.link')):
                    continue

                # New video: fetch full metadata (1 additional API call).
                video_info = fetch_video_info(video_entry['url'])
                if video_info is None:
                    continue

                result = store_video(storage_dir, video_info, subfolder=folder_name)
                if 'error' in result:
                    continue

                with app.app_context():
                    existing = db_models.YoutubeLibrary.query.filter_by(
                        hash=result['hash']).first()
                    if not existing:
                        existing = db_models.YoutubeLibrary(
                            hash=result['hash'],
                            hash_algorithm=HASH_ALGORITHM,
                            file_path=result['file_path'],
                            url=video_info.get('youtube_id', ''),
                        )
                        db_models.db.session.add(existing)
                        db_models.db.session.commit()
                added += 1

            # Refresh the last_sync timestamp in .channel.yaml.
            write_channel_yaml(folder_path, channel_id, channel_url,
                               conf.get('channel_name', ''))

            if added:
                print(f'[YouTube] {channel_name}: +{added} new video(s).')
            added_total += added

        ctx.update(1.0,
                   f'Synced {total} channel(s) — {added_total} new video(s) added.')
        print(f'[YouTube] Channel sync done — {added_total} new video(s) added.')

    def _check_and_submit_channel_sync():
        task_manager.submit('YouTube: sync channel updates', _task_sync_channels)

    schedule_task(app, interval_minutes=channel_update_interval,
                  fn=_check_and_submit_channel_sync)

    common_socket_events.show_loading_status('YouTube module ready!')
