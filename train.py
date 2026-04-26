"""
YouTube/train.py — Universal evaluator training contribution.

Exposes get_training_pairs() so that universal_train.py can collect
(chunk_embeddings, user_rating) pairs from user-rated YouTube videos.
"""

import os
import numpy as np
from omegaconf import OmegaConf

from modules.YouTube.fetcher import read_video_text


def get_training_pairs(cfg, text_embedder, status_callback=None):
    """
    Yield (chunk_embeddings, user_rating) pairs from user-rated YouTube videos.

    Each video's .link.meta file (or .link frontmatter body) is embedded
    with text_embedder.

    Parameters
    ----------
    cfg : OmegaConf DictConfig
    text_embedder : TextEmbedder
    status_callback : callable(str) or None

    Yields
    ------
    (np.ndarray of shape [chunks, dim], float)
    """
    import modules.YouTube.db_models as db_models

    storage_dir = OmegaConf.select(
        cfg, "YouTube.storage_directory",
        default="/mnt/project_config/modules/YouTube",
    )

    try:
        entries = db_models.YoutubeLibrary.query.filter(
            db_models.YoutubeLibrary.user_rating.isnot(None)
        ).all()
    except Exception as exc:
        print(f"[YouTube/train] DB query failed: {exc}")
        return

    total = len(entries)
    if total == 0:
        print("[YouTube/train] No user-rated videos found.")
        return

    print(f"[YouTube/train] {total} user-rated videos found.")
    if status_callback:
        status_callback(f"YouTube: found {total} user-rated videos.")

    for i, entry in enumerate(entries):
        if entry.file_path is None:
            continue

        full_path = os.path.join(storage_dir, entry.file_path)

        if not os.path.exists(full_path):
            continue

        try:
            # Combine .link frontmatter + .link.meta sidecar for richer training signal
            content = read_video_text(full_path)

            if not content or len(content.strip()) < 10:
                continue

            chunk_embeddings = text_embedder.embed_text(content)
            if chunk_embeddings is None or len(chunk_embeddings) == 0:
                continue

            yield (np.array(chunk_embeddings, dtype=np.float32), float(entry.user_rating))

        except Exception as exc:
            print(f"[YouTube/train] Error processing {entry.file_path}: {exc}")
            continue

        if status_callback:
            status_callback(f"YouTube: embedded {i + 1}/{total} videos...")
