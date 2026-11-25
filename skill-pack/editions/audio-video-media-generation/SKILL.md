---
name: audio-video-media-generation
description: "Use when generating, transforming, or analyzing audio/video media: songs, AI music prompts, spectrograms, Manim animations, ASCII videos, GIFs, and transcript-derived media content."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [media, audio, video, music, manim, gifs, youtube, spectrograms]
    related_skills: [creative-visual-artifacts]
---

# Audio and Video Media Generation

## Overview

This umbrella covers time-based media workflows: audio generation, songwriting prompts, music APIs, spectrogram analysis, mathematical animations, ASCII video transformations, GIF search, and YouTube transcript transformations.

## When to Use

- Write lyrics, Suno-style prompts, or generate music/audio.
- Use AudioCraft/MusicGen/AudioGen or HeartMuLa-style services.
- Analyze audio features such as mel, chroma, MFCC, and spectrograms.
- Create Manim/3Blue1Brown-style educational animations.
- Convert videos or audio into colored ASCII MP4/GIF outputs.
- Search/download GIFs or transform YouTube transcripts into summaries, posts, or scripts.

## Sub-workflows

### Music and audio generation
Separate lyrics, tags/style, generation call, and output verification. Keep provider limits and media paths explicit.

### Audio analysis
Use spectrogram/features tools for evidence before making claims about audio content.

### Educational animation
Plan scenes first, render incrementally, and inspect frames/videos for legibility.

### ASCII and GIF media
Treat conversion as a pipeline: input validation, render parameters, output file, and playback inspection.

### YouTube-derived content
Fetch transcripts or metadata with tools; do not invent unavailable transcript content.

## Common Pitfalls

1. Media generation succeeds only when an actual file/URL is produced and playable.
2. Large videos/audio may need downsampling or staged processing.
3. Transcript summaries must be grounded in fetched transcript text.

## Verification Checklist

- [ ] Input media or prompt source verified.
- [ ] Output file/URL exists.
- [ ] Rendered/played/analyzed result was checked before final response.
