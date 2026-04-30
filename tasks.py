"""
YouTube/tasks.py — Background and scheduled task functions.

All long-running work lives here so serve.py stays readable.
Every public task function has the signature  fn(ctx, deps)  where:
  - ctx   is the TaskContext supplied by task_manager (ctx.check(), ctx.update())
  - deps  is the Deps dataclass created once in init_socket_events

Helper functions that don't need a task context (upsert_video_db,
score_and_update, etc.) are plain functions that take deps explicitly.
"""

import datetime
import os
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

import modules.YouTube.db_models as db_models
from modules.YouTube.fetcher import (
    HASH_ALGORITHM,
    fetch_video_info,
    fetch_channel_recent_videos,
    iter_channel_dirs,
    find_channel_folder,
    make_channel_folder_name,
    store_video,
    write_channel_yaml,
    read_channel_yaml,
    read_video_text,
    fetch_subtitles,
    meta_has_transcript,
    append_transcript_to_meta,
)


# ── Deps ──────────────────────────────────────────────────────────────────────

@dataclass
class Deps:
    """All shared dependencies for YouTube background tasks.

    Created once in init_socket_events and passed to every task function.
    Using an explicit dataclass (rather than closures) keeps each function
    independently readable and testable.
    """
    app: Any
    storage_dir: str
    evaluator: Any
    text_embedder: Any
    engine: Any                        # YoutubeTextEngine instance
    channel_update_recent_count: int = 50
    transcript_backfill_batch: int = 50

    @property
    def cookies_file(self) -> str | None:
        """Return the cookies.txt path if it exists, else None."""
        p = os.path.join(self.storage_dir, 'cookies.txt')
        return p if os.path.isfile(p) else None


# ── DB helpers ────────────────────────────────────────────────────────────────

def upsert_video_db(app, result: dict, video_info: dict,
                    user_rating=None) -> int | None:
    """Create or update a YoutubeLibrary row. Returns the row id.

    Extracted here because all three import flows (single video, channel,
    playlist) do exactly the same thing after store_video() succeeds.
    """
    youtube_id = video_info.get('youtube_id', '')
    with app.app_context():
        row = db_models.YoutubeLibrary.query.filter_by(url=youtube_id).first() if youtube_id else None
        if row is None:
            row = db_models.YoutubeLibrary(
                hash=result['hash'],
                hash_algorithm=HASH_ALGORITHM,
                file_path=result['file_path'],
                url=youtube_id or None,
            )
            db_models.db.session.add(row)
        else:
            row.file_path = result['file_path']
        if user_rating is not None:
            row.user_rating = float(user_rating)
            row.user_rating_date = datetime.datetime.utcnow()
        db_models.db.session.commit()
        return row.id


def touch_last_viewed(video) -> None:
    """Stamp last_viewed = now and commit."""
    video.last_viewed = datetime.datetime.utcnow()
    db_models.db.session.commit()


# ── Import helpers ────────────────────────────────────────────────────────────

def resolve_subfolder(storage_dir: str, video_info: dict) -> str:
    """Return the channel subfolder name for a video.

    Looks for an existing channel folder first (by channel_id) so that
    newly fetched videos land in the same folder as previously imported ones.
    """
    subfolder = None
    if video_info.get('channel_id'):
        subfolder = find_channel_folder(storage_dir, video_info['channel_id'])
    return subfolder or make_channel_folder_name(
        video_info.get('author', '') or 'uncategorized',
        video_info.get('channel_id', ''),
    )


def _video_already_stored(storage_dir: str, youtube_id: str) -> bool:
    """Return True if a .link file for this youtube_id exists anywhere in storage_dir."""
    for root, _dirs, files in os.walk(storage_dir):
        if f'{youtube_id}.link' in files:
            return True
    return False


def import_video_list(ctx, deps: Deps, video_list: list,
                      user_rating=None, auto_update: bool = False,
                      on_progress=None) -> int:
    """Import a pre-fetched list of video entries, skipping already-stored ones.

    Shared logic for both handle_add_channel and handle_add_playlist.
    Returns the number of newly added videos.
    """
    total = len(video_list)
    added = 0
    for i, entry in enumerate(video_list):
        ctx.check()
        ctx.update(0.1 + 0.85 * (i / max(total, 1)),
                   f'Importing {i + 1}/{total}: {entry.get("title", "")[:50]}…')
        if on_progress:
            on_progress(f'Importing {i + 1}/{total}…')

        vid_url = entry.get('url', '')
        if not vid_url:
            continue

        # Fast filesystem check — skip if already stored
        vid_id = entry.get('youtube_id', '') or entry.get('id', '')
        if vid_id and _video_already_stored(deps.storage_dir, vid_id):
            continue

        video_info = fetch_video_info(vid_url)
        if video_info is None:
            continue

        result = store_video(deps.storage_dir, video_info,
                             subfolder=resolve_subfolder(deps.storage_dir, video_info),
                             auto_update=auto_update)
        if 'error' in result:
            continue

        upsert_video_db(deps.app, result, video_info, user_rating)
        added += 1

    return added


# ── Scoring ───────────────────────────────────────────────────────────────────

def heal_file_paths(storage_dir: str) -> int:
    """Reconcile DB file_path values against the actual filesystem.

    Walks the storage directory, builds a map of youtube_id → current
    relative path, then updates any DB rows whose file_path no longer
    matches.  Called automatically before scoring so that videos moved
    into category subfolders are never silently skipped.

    Returns the number of rows updated.
    """
    id_to_rel: dict[str, str] = {}
    for root, _dirs, files in os.walk(storage_dir):
        for fn in files:
            if fn.endswith('.link') and not fn.endswith('.link.meta'):
                vid_id = fn[:-5]  # strip .link suffix
                rel = os.path.relpath(os.path.join(root, fn), storage_dir)
                id_to_rel[vid_id] = rel

    if not id_to_rel:
        return 0

    rows = db_models.YoutubeLibrary.query.filter(
        db_models.YoutubeLibrary.url.in_(list(id_to_rel.keys()))
    ).all()
    updated = 0
    for row in rows:
        new_rel = id_to_rel.get(row.url)
        if new_rel and row.file_path != new_rel:
            row.file_path = new_rel
            updated += 1
    if updated:
        db_models.db.session.commit()
        print(f'[YouTube] heal_file_paths: updated {updated} stale path(s).')
    return updated


def score_video(file_path: str, deps: Deps) -> float | None:
    """Predict a rating for one video file. Returns None on failure or skip."""
    full_path = (os.path.join(deps.storage_dir, file_path)
                 if not os.path.isabs(file_path) else file_path)
    if not os.path.exists(full_path):
        return None
    try:
        body = read_video_text(full_path)
        if not body or len(body.strip()) < 10:
            return None
        chunk_embeddings = deps.text_embedder.embed_text(body)
        return float(deps.evaluator.predict([chunk_embeddings])[0])
    except Exception as exc:
        print(f'[YouTube] scoring error for {file_path}: {exc}')
        return None


def score_and_update(video_id: int, deps: Deps) -> None:
    """Score a single video by DB id and persist the rating immediately.

    Used right after a video is added so it appears rated without waiting
    for the next scheduled scoring cycle.
    """
    with deps.app.app_context():
        row = db_models.YoutubeLibrary.query.get(video_id)
        if row is None or row.file_path is None:
            return
        if row.model_rating is not None and row.model_hash == deps.evaluator.hash:
            return
        rating = score_video(row.file_path, deps)
        if rating is not None:
            row.model_rating = rating
            row.model_hash = deps.evaluator.hash
            db_models.db.session.commit()


def update_model_ratings(file_paths: list, deps: Deps) -> None:
    """Re-score a list of absolute file paths on demand.

    Called by CommonFilters when the user sorts by rating and some files
    lack a current model score.
    """
    for abs_path in file_paths:
        video_id = deps.engine.get_video_id(abs_path)
        if not video_id:
            continue
        row = db_models.YoutubeLibrary.query.filter_by(url=video_id).first()
        if row:
            score_and_update(row.id, deps)


def task_score_videos(ctx, deps: Deps) -> None:
    """Score up to 1000 unscored or stale videos in one batch.

    Scheduled every score_interval_minutes. A single commit at the end
    keeps the DB round-trips minimal. The task itself decides whether
    there is work to do, so the scheduler always submits it.
    """
    # Heal any stale file_path values first so moved folders don't get skipped.
    heal_file_paths(deps.storage_dir)

    current_hash = deps.evaluator.hash
    if current_hash is None:
        return
    videos = db_models.YoutubeLibrary.query.filter(
        (db_models.YoutubeLibrary.model_rating.is_(None)) |
        (db_models.YoutubeLibrary.model_hash != current_hash)
    ).all()
    if not videos:
        return
    videos_to_process = videos[:1000]

    total_in_queue = len(videos)
    total_to_process = len(videos_to_process)
    
    print(f'[YouTube] Scoring {total_to_process} video(s) with evaluator {current_hash}…')
    scored = 0
    for i, video in enumerate(videos_to_process):
        ctx.check()
        ctx.update((i + 1) / total_to_process, f'Scoring video {i + 1}/{total_to_process}. With {total_in_queue - total_to_process} to go in queue.')
        if video.file_path is None:
            continue
        rating = score_video(video.file_path, deps)
        if rating is not None:
            video.model_rating = rating
            video.model_hash = current_hash
            scored += 1
    db_models.db.session.commit()
    print(f'[YouTube] Scoring done — {scored}/{total_to_process} scored.')


# ── Migration tasks ───────────────────────────────────────────────────────────

def find_folders_to_migrate(storage_dir: str) -> list[tuple]:
    """Return (old_rel, new_rel, old_abs_path) for channel folders missing the ID suffix.

    Searches recursively so channels inside category subfolders are included.
    old_rel / new_rel are both relative to storage_dir, preserving the
    category prefix (e.g. 'Science/Action Lab' → 'Science/Action Lab (UCxxx)').
    """
    to_rename = []
    for rel, abs_path, conf in iter_channel_dirs(storage_dir):
        channel_id = conf.get('channel_id', '')
        basename = os.path.basename(rel)
        if not channel_id or f'({channel_id[:8]})' in basename:
            continue
        channel_name = conf.get('channel_name', '') or basename
        new_basename = make_channel_folder_name(channel_name, channel_id)
        parent_rel = os.path.dirname(rel)  # '' for top-level, 'Science' for nested
        new_rel = os.path.join(parent_rel, new_basename) if parent_rel else new_basename
        to_rename.append((rel, new_rel, abs_path))
    return to_rename


def task_migrate_channel_folders(ctx, deps: Deps) -> None:
    """Rename old channel folders to include the channel ID suffix."""
    to_rename = find_folders_to_migrate(deps.storage_dir)
    if not to_rename:
        return
    total = len(to_rename)
    print(f'[YouTube] Migrating {total} channel folder(s) to new naming format…')
    for i, (old_rel, new_rel, old_path) in enumerate(to_rename):
        ctx.check()
        ctx.update((i + 1) / total, f'Renaming {old_rel} → {new_rel}')
        new_path = os.path.join(deps.storage_dir, new_rel)
        if os.path.exists(new_path):
            print(f'[YouTube] Skipping {old_rel} → {new_rel}: target exists')
            continue
        try:
            os.rename(old_path, new_path)
            with deps.app.app_context():
                videos = db_models.YoutubeLibrary.query.filter(
                    db_models.YoutubeLibrary.file_path.like(f'{old_rel}/%')
                ).all()
                for video in videos:
                    video.file_path = new_rel + video.file_path[len(old_rel):]
                db_models.db.session.commit()
            print(f'[YouTube] Renamed: {old_rel} → {new_rel}')
        except OSError as exc:
            print(f'[YouTube] Failed to rename {old_rel}: {exc}')
    ctx.update(1.0, f'Migrated {total} folder(s).')


def task_migrate_png_previews(ctx, deps: Deps) -> None:
    """Convert leftover .preview.png thumbnails to JPEG and delete the originals."""
    png_files = [
        os.path.join(root, fn)
        for root, _dirs, files in os.walk(deps.storage_dir)
        for fn in files if fn.endswith('.preview.png')
    ]
    if not png_files:
        return
    total = len(png_files)
    print(f'[YouTube] Converting {total} PNG preview(s) to JPEG…')
    for i, png_path in enumerate(png_files):
        ctx.check()
        ctx.update((i + 1) / total, f'Converting preview {i + 1}/{total}…')
        jpg_path = png_path[:-4] + '.jpg'
        try:
            img = Image.open(png_path).convert('RGB')
            img.thumbnail((512, 512), Image.LANCZOS)
            img.save(jpg_path, 'JPEG', quality=85, optimize=True)
            os.remove(png_path)
        except Exception as exc:
            print(f'[YouTube] Failed to convert {png_path}: {exc}')
    print(f'[YouTube] PNG preview migration done ({total} file(s)).')


# ── Scheduled tasks ───────────────────────────────────────────────────────────

def task_sync_channels(ctx, deps: Deps) -> None:
    """Check every tracked channel for new videos and store any that are missing.

    Cost model: 1 flat-extraction API call per channel + 1 full-metadata call
    only for each genuinely new video (fast filesystem check for the rest).
    """
    channel_dirs = [
        (rel, abs_path, conf)
        for rel, abs_path, conf in iter_channel_dirs(deps.storage_dir)
        if conf.get('channel_url')
    ]

    if not channel_dirs:
        return

    total = len(channel_dirs)
    added_total = 0
    print(f'[YouTube] Channel sync started — {total} channel(s) tracked.')

    for i, (folder_name, folder_path, conf) in enumerate(channel_dirs):
        ctx.check()
        channel_name = conf.get('channel_name') or folder_name
        ctx.update(i / total, f'Syncing {i + 1}/{total}: {channel_name}')

        if not conf.get('auto_update', True):
            continue

        channel_url = conf.get('channel_url', '')
        channel_id = conf.get('channel_id', '')
        if not channel_url:
            continue

        recent = fetch_channel_recent_videos(channel_url,
                                             count=deps.channel_update_recent_count)
        added = 0
        for video_entry in recent:
            ctx.check()
            vid_id = video_entry.get('youtube_id', '')
            if not vid_id:
                continue
            if os.path.exists(os.path.join(folder_path, f'{vid_id}.link')):
                continue
            video_info = fetch_video_info(video_entry['url'])
            if video_info is None:
                continue
            result = store_video(deps.storage_dir, video_info, subfolder=folder_name)
            if 'error' in result:
                continue
            with deps.app.app_context():
                vid_id_str = video_info.get('youtube_id', '')
                if not db_models.YoutubeLibrary.query.filter_by(url=vid_id_str).first():
                    db_models.db.session.add(db_models.YoutubeLibrary(
                        hash=result['hash'],
                        hash_algorithm=HASH_ALGORITHM,
                        file_path=result['file_path'],
                        url=vid_id_str or None,
                    ))
                    db_models.db.session.commit()
            added += 1

        write_channel_yaml(folder_path, channel_id, channel_url,
                           conf.get('channel_name', ''), auto_update=None)
        if added:
            print(f'[YouTube] {channel_name}: +{added} new video(s).')
        added_total += added

    ctx.update(1.0, f'Synced {total} channel(s) — {added_total} new video(s) added.')
    print(f'[YouTube] Channel sync done — {added_total} new video(s) added.')


def task_backfill_transcripts(ctx, deps: Deps) -> None:
    """Fetch subtitles for .meta files that are missing a transcript section."""
    candidates = []
    for root, _dirs, files in os.walk(deps.storage_dir):
        for fn in files:
            if not fn.endswith('.link.meta'):
                continue
            meta_path = os.path.join(root, fn)
            if meta_has_transcript(meta_path):
                continue
            vid_id = fn.rsplit('.link.meta', 1)[0]
            if vid_id:
                candidates.append((vid_id, meta_path))

    if not candidates:
        return

    batch = candidates[:deps.transcript_backfill_batch]
    total = len(batch)
    added = 0
    print(f'[YouTube] Transcript backfill: {total} video(s) to process '
          f'({len(candidates)} total remaining).')

    for i, (vid_id, meta_path) in enumerate(batch):
        ctx.check()
        ctx.update((i + 1) / total, f'Fetching transcript {i + 1}/{total}…')
        try:
            transcript = fetch_subtitles(vid_id, cookies_file=deps.cookies_file)
            if transcript:
                append_transcript_to_meta(meta_path, transcript)
                added += 1
        except Exception as exc:
            print(f'[YouTube] transcript backfill error for {vid_id}: {exc}')

    ctx.update(1.0, f'Backfilled {added}/{total} transcript(s).')
    print(f'[YouTube] Transcript backfill done — {added}/{total} fetched.')
