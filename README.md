# YouTube Module

An Anagnorisis module that treats YouTube purely as a video CDN (content delivery network) - it creates a lean local representation of each video you care about and hands all search, ranking, and recommendations over to Anagnorisis's own algorithms. No YouTube homepage, no autoplay rabbit-holes, no engagement-maximising suggestions.

This can be especially useful if you want to build a safe, distraction-free zone for a child, only the videos you explicitly add are visible, ordered by your own criteria. Or use it yourself to break bad watching habits and build a curated library of content that actually matters to you.

## How it works

You add videos or entire channels. The module fetches metadata (title, channel, description, tags, duration, thumbnail) from YouTube and stores it locally as a small `.link` file. The video itself is never downloaded — it is streamed on demand directly from YouTube's CDN when you press play, at the quality you choose. Everything else — searching, sorting, recommendations, ratings — runs locally without any input from YouTube.

## Local file structure

Each video is stored as a set of files inside the configured storage directory:

```
<storage_directory>/
  <channel-name>/
    .channel.yaml                   # channel ID, URL, last sync timestamp
    <youtube-id>.link               # YAML front-matter: title, author, duration, publish date, URL
    <youtube-id>.link.meta          # plain-text metadata for semantic search (description, tags, subtitles)
    <youtube-id>.link.preview.jpg   # thumbnail image
```

## Playback

Videos stream through a server-side proxy that forwards HTTP Range requests to YouTube's CDN, so seeking, duration display, and speed-control browser extensions all work correctly. Quality can be changed while a video is playing (360p – 4K). Because YouTube caps pre-muxed progressive streams at around 720p for most videos, selecting 1080p or higher will deliver the best pre-muxed quality available.

## Authentication

Some videos require you to be signed in to YouTube. To sing-in you need to provide the **cookies.txt** file generated in Netscape format while signed in to YouTube. After providing the file, it is gonna be stored locally as `cookies.txt` inside the storage directory.
