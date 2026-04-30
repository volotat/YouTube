"""
YouTube/engine.py — Text search and embedding adapter.

YoutubeTextEngine bridges the yt-dlp data model (.link front-matter +
.link.meta sidecars) with the FileManager / CommonFilters interface
expected by the rest of the framework.

Every other module has an engine.py; this is the canonical home for it.
"""

import os

import numpy as np
import torch

from modules.YouTube.fetcher import (
    HASH_ALGORITHM,
    _blake2b,
    parse_frontmatter,
    read_video_text,
)


class YoutubeTextEngine:
    def __init__(self, text_embedder, cache, storage_dir, cfg=None):
        self._emb = text_embedder
        self._cache = cache
        self.storage_dir = storage_dir
        self.cfg = cfg
        self._emb_dim_cache = None

    # ── FileManager / CommonFilters interface ─────────────────────────────

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

    # ── Front-matter reading (cached) ─────────────────────────────────────

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

    def get_video_id(self, path: str) -> str:
        """Return the stable youtube_id from frontmatter (cached via get_frontmatter)."""
        return self.get_frontmatter(path).get('youtube_id', '')

    def get_title_and_url(self, path: str) -> tuple[str, str]:
        meta = self.get_frontmatter(path)
        return (meta.get('title', os.path.basename(path)), meta.get('url', ''))

    # ── Embedding interface ───────────────────────────────────────────────

    def process_text(self, text: str):
        return np.array(self._emb.embed_text(text))

    def process_query(self, text: str):
        return self.process_text(text)

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
                body = read_video_text(path)
                emb = self._emb.embed_text(body or '')
                vec = (np.array(emb).mean(axis=0)
                       if emb is not None and len(emb)
                       else np.zeros(self._emb_dim()))
            except Exception:
                vec = np.zeros(self._emb_dim())
            self._cache.set(cache_key, vec)
            rows.append(vec)
        return (torch.tensor(np.stack(rows), dtype=torch.float32)
                if rows else torch.zeros((0, self._emb_dim())))

    def compare(self, embeds_files, embeds_text):
        ef = embeds_files.numpy() if isinstance(embeds_files, torch.Tensor) else np.array(embeds_files)
        qt = np.array(embeds_text)
        if qt.ndim > 1:
            qt = qt.mean(axis=0)
        norms = np.linalg.norm(ef, axis=1) * np.linalg.norm(qt)
        return np.dot(ef, qt) / np.maximum(norms, 1e-8)

    def _emb_dim(self) -> int:
        if self._emb_dim_cache is None:
            try:
                self._emb_dim_cache = self._emb.embedding_dim or 1024
            except Exception:
                self._emb_dim_cache = 1024
        return self._emb_dim_cache


# ── Preview text helper ───────────────────────────────────────────────────────

def get_text_preview(file_path: str) -> str:
    """Return a short preview string for display in the file grid."""
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
