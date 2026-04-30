import StarRatingComponent from '/modules/StarRating.js';
import FolderViewComponent from '/modules/FolderViewComponent.js';
import FileGridComponent from '/modules/FileGridComponent.js';
import PaginationComponent from '/modules/PaginationComponent.js';
import SearchBarComponent from '/modules/SearchBarComponent.js';
import ContextMenuComponent from '/modules/ContextMenuComponent.js';
import createModuleMetaEditors from '/modules/ModuleMetaEditors.js';

(function() {
  const PAGE_LIMIT = 24;

  // ── URL state ─────────────────────────────────────────────────────────
  const urlParams = new URLSearchParams(window.location.search);
  let page = parseInt(urlParams.get('page')) || 1;
  let path = decodeURIComponent(urlParams.get('path') || '');

  // ── Helpers ───────────────────────────────────────────────────────────

  function timeAgo(isoString) {
    const diff = Math.floor((Date.now() - new Date(isoString)) / 1000);
    if (diff < 60)             return `${diff}s ago`;
    if (diff < 3600)           return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400)          return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 86400 * 30)     return `${Math.floor(diff / 86400)}d ago`;
    if (diff < 86400 * 365)    return `${Math.floor(diff / (86400 * 30))}mo ago`;
    return `${Math.floor(diff / (86400 * 365))}y ago`;
  }

  // ── Meta editors ──────────────────────────────────────────────────────
  const { openMetaEditor, openFullDescription } = createModuleMetaEditors(socket, 'YouTube');

  // ── Context menu ──────────────────────────────────────────────────────
  const ctxMenu = new ContextMenuComponent();

  function createContextMenu(fileData, event) {
    const info = fileData.file_info || {};
    ctxMenu.show(event.pageX, event.pageY, [
      {
        label: 'Play',
        icon: 'fas fa-play',
        action: () => openPlayer(fileData),
      },
      {
        label: 'Edit .meta file',
        icon: 'fas fa-file-pen',
        action: () => openMetaEditor(fileData),
      },
      {
        label: 'Show full search description',
        icon: 'fas fa-info-circle',
        action: () => openFullDescription(fileData),
      },
    ]);
  }

  // ── Render helpers ────────────────────────────────────────────────────

  function renderPreview(fileData) {
    const info = fileData.file_info || {};
    const container = document.createElement('div');
    container.className = 'yt-card';

    // Thumbnail
    const img = document.createElement('img');
    img.className = 'yt-card-thumb';
    if (info.preview_path) {
      img.src = '/youtube_files/' + info.preview_path;
    } else {
      img.src = '/static/images/placeholder.png';
    }
    img.alt = info.title || '';
    img.loading = 'lazy';
    container.appendChild(img);

    // Duration badge
    if (info.duration) {
      const badge = document.createElement('span');
      badge.className = 'yt-card-duration';
      badge.textContent = info.duration;
      container.appendChild(badge);
    }

    return container;
  }

  function renderCustomData(fileData) {
    const info = fileData.file_info || {};
    const wrap = document.createElement('div');
    wrap.className = 'yt-card-info';

    const title = document.createElement('div');
    title.className = 'yt-card-title';
    title.textContent = info.title || fileData.base_name || fileData.file_path;
    wrap.appendChild(title);

    if (info.author) {
      const author = document.createElement('p');
      author.className = 'file-info yt-card-author';
      author.textContent = info.author;
      wrap.appendChild(author);
    }

    if (info.publish_date) {
      const pub = document.createElement('p');
      pub.className = 'file-info file-publish-date';
      pub.innerHTML = `<b>Published:</b>&nbsp;${info.publish_date}`;
      wrap.appendChild(pub);
    }

    if (info.duration) {
      const dur = document.createElement('p');
      dur.className = 'file-info file-length';
      dur.innerHTML = `<b>Length:</b>&nbsp;${info.duration}`;
      wrap.appendChild(dur);
    }

    if (info.last_viewed) {
      const lv = document.createElement('p');
      lv.className = 'file-info file-last-played';
      lv.innerHTML = `<b>Last viewed:</b>&nbsp;${timeAgo(info.last_viewed)}`;
      wrap.appendChild(lv);
    }

    const userRating = document.createElement('p');
    userRating.className = 'file-info file-user-rating';
    userRating.innerHTML = `<b>User rating:</b>&nbsp;${info.user_rating !== null && info.user_rating !== undefined ? info.user_rating.toFixed(1) : 'N/A'}`;
    wrap.appendChild(userRating);

    const modelRating = document.createElement('p');
    modelRating.className = 'file-info file-model-rating';
    modelRating.innerHTML = `<b>Model rating:</b>&nbsp;${info.model_rating !== null && info.model_rating !== undefined ? info.model_rating.toFixed(1) : 'N/A'}`;
    wrap.appendChild(modelRating);

    return wrap;
  }

  // ── Player modal ──────────────────────────────────────────────────────

  let _currentFileData = null;
  let _playerRating = null;
  let _currentQuality = '1080';

  function _loadStream(ytId, quality) {
    const video = document.getElementById('yt-modal-video');
    const errorEl = document.getElementById('yt-modal-error');
    errorEl.style.display = 'none';
    errorEl.innerHTML = '';
    const savedTime = video.currentTime || 0;

    socket.emit('emit_youtube_page_get_stream_url', {
      youtube_id: ytId,
      quality: quality,
      hash: _currentFileData ? _currentFileData.hash : '',
    }, (response) => {
      if (response && response.url) {
        video.src = response.url;
        video.load();
        if (savedTime > 1) {
          video.addEventListener('loadedmetadata', function _seek() {
            video.removeEventListener('loadedmetadata', _seek);
            video.currentTime = savedTime;
          });
        }
        video.play().catch(() => {});
      } else {
        errorEl.textContent = 'Could not start stream: ' + (response && response.error || 'unknown error');
        errorEl.style.display = 'block';
      }
    });
  }

  function openPlayer(fileData) {
    const info = fileData.file_info || {};
    const ytId = info.youtube_id || '';
    if (!ytId) { alert('No YouTube ID found for this video.'); return; }

    _currentFileData = fileData;

    const modal = document.getElementById('yt-player-modal');
    const video = document.getElementById('yt-modal-video');
    const titleEl = document.getElementById('yt-modal-title');
    const authorEl = document.getElementById('yt-modal-author');
    const ratingContainer = document.getElementById('yt-modal-rating-container');

    titleEl.textContent = info.title || '';
    authorEl.textContent = info.author || '';

    // Rating widget
    ratingContainer.innerHTML = '';
    _playerRating = new StarRatingComponent({
      initialRating: info.user_rating,
      callback: (val) => {
        socket.emit('emit_youtube_page_set_rating', {
          hash: fileData.hash,
          file_path: fileData.file_path,
          rating: val,
        });
      },
    });
    ratingContainer.appendChild(_playerRating.issueNewHtmlComponent({ containerType: 'span', isActive: true }));

    // Show modal
    modal.classList.add('is-active');
    video.src = '';
    video.poster = info.preview_path ? '/youtube_files/' + info.preview_path : '';
    document.getElementById('yt-quality-select').value = _currentQuality;

    _loadStream(ytId, _currentQuality);

    // Mark viewed
    if (info.youtube_id) {
      socket.emit('emit_youtube_page_mark_viewed', { youtube_id: info.youtube_id });
    }
  }

  function closePlayer() {
    const modal = document.getElementById('yt-player-modal');
    const video = document.getElementById('yt-modal-video');
    const errorEl = document.getElementById('yt-modal-error');
    modal.classList.remove('is-active');
    video.pause();
    video.src = '';
    video.load();
    errorEl.style.display = 'none';
    errorEl.innerHTML = '';
    _currentFileData = null;
  }

  // ── Video error / recovery handler ────────────────────────────────────
  {
    const _video = document.getElementById('yt-modal-video');
    const _errorEl = document.getElementById('yt-modal-error');

    // Hide the error as soon as the video can actually play
    _video.addEventListener('canplay', () => {
      _errorEl.style.display = 'none';
      _errorEl.innerHTML = '';
    });

    _video.addEventListener('error', async () => {
      // Ignore spurious errors when no video is open or src was just cleared
      if (!_currentFileData || !_video.src || _video.src === window.location.href) return;
      const ytId = (_currentFileData.file_info || {}).youtube_id || '';
      if (!ytId) return;
      _errorEl.style.display = 'block';
      _errorEl.textContent = 'Checking stream…';
      try {
        const r = await fetch(`/youtube_stream/${ytId}?quality=${_currentQuality}&info=1`);
        const data = await r.json();
        if (r.status === 403 && data.needs_cookies) {
          _errorEl.innerHTML =
            '<strong>YouTube requires sign-in for this video.</strong><br><br>' +
            '<button class="button is-link is-small" id="yt-error-signin-btn" style="margin-bottom:.5rem">Upload cookies.txt</button>' +
            '<br><span style="font-size:.85rem; color:#aaa">Export cookies from your browser while signed into YouTube (Netscape format).</span>';
          document.getElementById('yt-error-signin-btn').addEventListener('click', openCookiesModal);
        } else {
          _errorEl.textContent = 'Could not load video stream: ' + (data.error || 'unknown error');
        }
      } catch(e) {
        _errorEl.textContent = 'Could not load video stream.';
      }
    });
  }

  // ── Add-video modal helpers ───────────────────────────────────────────

  let _addVideoRating = null;
  let _addVideoStar = null;
  let _addChannelRating = null;
  let _addChannelStar = null;
  let _addPlaylistRating = null;
  let _addPlaylistStar = null;

  function openAddVideoModal() {
    const modal = document.getElementById('yt_add_video_modal');
    document.getElementById('yt_add_video_url').value = '';
    _addVideoRating = null;

    const ratingContainer = document.getElementById('yt_add_video_rating');
    ratingContainer.innerHTML = '';
    _addVideoStar = new StarRatingComponent({
      initialRating: null,
      callback: (val) => { _addVideoRating = val; },
    });
    ratingContainer.appendChild(_addVideoStar.issueNewHtmlComponent({ containerType: 'span', isActive: true }));

    modal.classList.add('is-active');
  }

  function closeAddVideoModal() {
    document.getElementById('yt_add_video_modal').classList.remove('is-active');
  }

  function confirmAddVideo() {
    const url = document.getElementById('yt_add_video_url').value.trim();
    if (!url) return;
    socket.emit('emit_youtube_page_add_video', {
      url: url,
      user_rating: _addVideoRating,
    });
    closeAddVideoModal();
  }

  function openAddChannelModal() {
    const modal = document.getElementById('yt_add_channel_modal');
    document.getElementById('yt_add_channel_url').value = '';
    _addChannelRating = null;

    const ratingContainer = document.getElementById('yt_add_channel_rating');
    if (ratingContainer) {
      ratingContainer.innerHTML = '';
      _addChannelStar = new StarRatingComponent({
        initialRating: null,
        callback: (val) => { _addChannelRating = val; },
      });
      ratingContainer.appendChild(_addChannelStar.issueNewHtmlComponent({ containerType: 'span', isActive: true }));
    }

    modal.classList.add('is-active');
  }

  function closeAddChannelModal() {
    document.getElementById('yt_add_channel_modal').classList.remove('is-active');
  }

  function confirmAddChannel() {
    const url = document.getElementById('yt_add_channel_url').value.trim();
    if (!url) return;
    socket.emit('emit_youtube_page_add_channel', {
      url: url,
      user_rating: _addChannelRating,
    });
    closeAddChannelModal();
  }

  function openAddPlaylistModal() {
    const modal = document.getElementById('yt_add_playlist_modal');
    document.getElementById('yt_add_playlist_url').value = '';
    _addPlaylistRating = null;

    const ratingContainer = document.getElementById('yt_add_playlist_rating');
    ratingContainer.innerHTML = '';
    _addPlaylistStar = new StarRatingComponent({
      initialRating: null,
      callback: (val) => { _addPlaylistRating = val; },
    });
    ratingContainer.appendChild(_addPlaylistStar.issueNewHtmlComponent({ containerType: 'span', isActive: true }));

    modal.classList.add('is-active');
  }

  function closeAddPlaylistModal() {
    document.getElementById('yt_add_playlist_modal').classList.remove('is-active');
  }

  function confirmAddPlaylist() {
    const url = document.getElementById('yt_add_playlist_url').value.trim();
    if (!url) return;
    socket.emit('emit_youtube_page_add_playlist', {
      url: url,
      user_rating: _addPlaylistRating,
    });
    closeAddPlaylistModal();
  }

  // ── YouTube cookies upload ────────────────────────────────────────────

  function openCookiesModal() {
    const modal = document.getElementById('yt-cookies-modal');
    document.getElementById('yt-cookies-status').textContent = '';
    modal.classList.add('is-active');
  }

  function closeCookiesModal() {
    document.getElementById('yt-cookies-modal').classList.remove('is-active');
  }

  function _uploadCookies(file) {
    const statusEl = document.getElementById('yt-cookies-status');
    if (!file) return;
    statusEl.textContent = 'Uploading…';
    const reader = new FileReader();
    reader.onload = () => {
      socket.emit('emit_youtube_page_upload_cookies', { content: reader.result }, (resp) => {
        if (resp && resp.ok) {
          statusEl.innerHTML = '<span class="has-text-success"><strong>\u2705 cookies.txt saved.</strong> You can now play videos.</span>';
          document.getElementById('yt_signin_btn').textContent = '\u2713 cookies.txt uploaded';
        } else {
          statusEl.innerHTML = `<span class="has-text-danger"><strong>Error:</strong> ${resp ? resp.error : 'Unknown error'}</span>`;
        }
      });
    };
    reader.readAsText(file);
  }

  // ── Page initialisation ───────────────────────────────────────────────

  $(document).ready(function() {

    // ── Search bar ────────────────────────────────────────────────────
    const searchBar = new SearchBarComponent({
      container: '#search_bar_container',
      enableModes: ['file-name', 'semantic-metadata'],
      showOrder: true,
      showTemperature: true,
      temperatures: [0, 0.2, 1, 2],
      keywords: ['recommendation', 'recent', 'random', 'rating'],
      autoSyncUrl: true,
      ensureDefaultsInUrl: true,
    });
    const searchState = searchBar.getState();

    // ── Sidebar buttons ─────────────────────────────────────────────
    document.getElementById('yt_add_video_btn').addEventListener('click', openAddVideoModal);
    document.getElementById('yt_add_channel_btn').addEventListener('click', openAddChannelModal);
    document.getElementById('yt_add_playlist_btn').addEventListener('click', openAddPlaylistModal);
    document.getElementById('yt_signin_btn').addEventListener('click', openCookiesModal);

    // Cookies modal wiring
    document.querySelectorAll('.yt-cookies-close').forEach(el =>
      el.addEventListener('click', closeCookiesModal));
    const dropZone = document.getElementById('yt-cookies-drop');
    const fileInput = document.getElementById('yt-cookies-file');
    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      if (e.dataTransfer.files.length) _uploadCookies(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', () => { if (fileInput.files.length) _uploadCookies(fileInput.files[0]); });

    // Add-video modal wiring
    document.querySelectorAll('.yt-add-video-close').forEach(el =>
      el.addEventListener('click', closeAddVideoModal));
    document.getElementById('yt_add_video_confirm').addEventListener('click', confirmAddVideo);

    // Add-channel modal wiring
    document.querySelectorAll('.yt-add-channel-close').forEach(el =>
      el.addEventListener('click', closeAddChannelModal));
    document.getElementById('yt_add_channel_confirm').addEventListener('click', confirmAddChannel);

    // Add-playlist modal wiring
    document.querySelectorAll('.yt-add-playlist-close').forEach(el =>
      el.addEventListener('click', closeAddPlaylistModal));
    document.getElementById('yt_add_playlist_confirm').addEventListener('click', confirmAddPlaylist);

    // Player modal close
    document.getElementById('yt-player-modal-bg').addEventListener('click', closePlayer);
    document.getElementById('yt-player-modal-close').addEventListener('click', closePlayer);

    // Quality selector
    document.getElementById('yt-quality-select').addEventListener('change', (e) => {
      _currentQuality = e.target.value;
      if (!_currentFileData) return;
      const ytId = (_currentFileData.file_info || {}).youtube_id || '';
      if (!ytId) return;
      _loadStream(ytId, _currentQuality);
    });

    // ── Folders ──────────────────────────────────────────────────────
    socket.emit('emit_youtube_page_get_folders', { path: path }, (response) => {
      const folderView = new FolderViewComponent(
        response.folders, response.folder_path, false);
      document.getElementById('yt_folder_tree').appendChild(folderView.getDOMElement());
    });

    // ── Files ────────────────────────────────────────────────────────
    socket.emit('emit_youtube_page_get_files', {
      path: path,
      pagination: (page - 1) * PAGE_LIMIT,
      limit: PAGE_LIMIT,
      text_query: searchState.text_query,
      seed: searchState.seed,
      mode: searchState.mode,
      order: searchState.order,
      temperature: searchState.temperature,
    }, (response) => {
      // Grid
      const fileGridComponent = new FileGridComponent({
        containerId: '#youtube_files_grid_container',
        filesData: response.files_data,
        renderPreviewContent: renderPreview,
        renderCustomData: renderCustomData,
        handleFileClick: openPlayer,
        minTileWidth: '18rem',
        onContextMenu: createContextMenu,
        onMetaOpen: openMetaEditor,
      });

      // Pagination
      const totalPages = Math.ceil(response.total_files / PAGE_LIMIT);
      if (totalPages > 1) {
        const paginationUrlPattern = '?' + urlParams.toString();
        ['#yt-pagination-top', '#yt-pagination-bottom'].forEach(id => {
          new PaginationComponent({
            containerId: id,
            currentPage: page,
            totalPages: totalPages,
            urlPattern: paginationUrlPattern,
          });
        });
      }
    });

    // ── Socket listeners ────────────────────────────────────────────
    socket.on('emit_show_search_status', (status) => {
      document.querySelectorAll('.image-search-status').forEach(el => {
        el.innerHTML = status;
      });
    });

    socket.on('emit_youtube_page_video_added', (data) => {
      if (data.error) {
        console.error('Add video error:', data.error);
        return;
      }
      // Reload to show new video
      window.location.reload();
    });

    socket.on('emit_youtube_page_channel_imported', (data) => {
      document.getElementById('yt_import_status').textContent =
        `Import complete — ${data.count} videos.`;
      window.location.reload();
    });

    socket.on('emit_youtube_page_playlist_imported', (data) => {
      document.getElementById('yt_import_status').textContent =
        `Playlist import complete — ${data.count} videos.`;
      window.location.reload();
    });

    socket.on('emit_youtube_page_import_progress', (data) => {
      document.getElementById('yt_import_status').textContent = data.message || '';
    });
  });
})();
