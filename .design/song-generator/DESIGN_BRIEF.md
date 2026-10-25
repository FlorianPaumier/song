# Design Brief: Local Song Generator

## Problem

The creator wants to turn a song description, optionally with their own structured lyrics, into full local songs without managing model settings or losing track of generated takes. They also want to queue many variations (for example, ten or more complete songs) from one description, add more batches later, and leave the machine to work through them. Existing generator interfaces put too many modes, controls, and navigation layers between writing, listening, and finding a prior song.

## Solution

A single-screen local studio. The left side holds a required description and optional lyrics; the right side is a searchable library of songs and takes. A persistent player along the bottom makes comparison immediate. The Windows machine runs ACE-Step at its existing fixed LAN API address (`http://192.168.40.55:8001`); the Mac accesses the application over the local network. Everything stays local after model setup.

## Experience Principles

1. **Creation before configuration** -- Description is the only required input. Lyrics are optional; when absent, ACE-Step generates them. Detect tempo and section structure automatically; show the interpretation without requiring advanced fields.
2. **Find and compare quickly** -- Search, recent-first ordering, favorites, and clear take grouping are more important than artwork or elaborate workspaces.
3. **Honest feedback** -- Show queued/running/failed/completed states, the requested BPM and duration, and distinguish generated files from release-ready exports.

## Aesthetic Direction

- **Philosophy**: Quiet local studio, not a social music platform.
- **Tone**: Simple, focused, and calm.
- **Reference points**: The user's Suno screenshot: dark surface, side-by-side creation and library, bottom playback bar.
- **Anti-references**: Suno's large sidebar, credits/upgrades, multiple creation modes, inspiration/voice/audio add-ons, dense filter bars, and decorative artwork as the primary way to identify songs.

## Existing Patterns

The project directory was empty when this brief was written. No components, routes, typography, color tokens, Tailwind configuration, or UI dependencies exist yet.

## Component Inventory

| Component | Status | Notes |
| --- | --- | --- |
| Studio shell | New | Two main columns on desktop: create and library. |
| Description field | New | Free text; parses explicit BPM such as `200 BPM`. |
| Lyrics editor | New | Optional multiline input; accepts readable headings and shorthand repeats. Empty means generate lyrics from the description. |
| Interpretation preview | New | Compact summary of detected BPM, duration target, and expanded section order before generation. |
| Generate control | New | Choose a quantity (2 by default), with 10 as a quick choice and larger custom quantities allowed; adds jobs to a persistent queue. |
| Queue view | New | Shows current song, pending count, completed count, failures, and controls to pause or cancel pending jobs. |
| Song/take list | New | Groups takes under a song; title proposed automatically and editable. |
| Search and favorites | New | Instant search across title, description, and lyrics; recent-first default. |
| Persistent audio player | New | Play/pause, seek, duration, volume, and active take name. |
| Take actions | New | Favorite, rename, regenerate whole song, export WAV/MP3. |
| System status | New | Small indicator for ACE-Step connection and active model; details tucked away. |

## Key Interactions

1. Enter a description and optionally lyrics. If the description includes an explicit tempo, parse it into ACE-Step's `bpm` parameter; do not rely on the model to infer it from prose alone. Preserve the original description. Make clear that requested BPM is a target, not a measured guarantee.
2. If lyrics are supplied, parse them into section tags, expand repeat shorthand, and remove non-sung speaker labels. Show the resulting outline for review; keep the original text available for editing. If lyrics are empty, use ACE-Step's description-driven generation to create lyrics and song metadata for each variation.
3. Choose a quantity (default 2; 10 as a quick choice; custom larger counts allowed) and add a persistent local batch using the current validated default (ACE-Step XL SFT + 5Hz LM 4B, subject to runtime availability), requesting 2:30–4:00 and WAV output. Show an estimated duration/storage warning before unusually large batches. Run one song at a time on the GPU, with a distinct random seed per song; do not send one oversized ACE-Step batch or switch models mid-batch.
4. Add each requested song to the library immediately as queued, then update its state individually. Additional batches append to the same queue. Show overall and per-batch progress such as `3 of 10 complete`; the user can leave the page and return. A failed song remains visible with a useful error and retry action, without losing completed songs. Pause the queue and cancel jobs that have not started; do not imply an active ACE-Step job can be safely interrupted unless supported.
5. Selecting a take plays it in the persistent player without losing the current draft. Search filters the library instantly; favorites are one click away.
6. Export the selected WAV or derive an MP3 from it locally. Keep source WAV, prompt, lyrics, parsed metadata, model names, seed, and generation date with every take.

## Responsive Behavior

- **Desktop/Mac browser**: Creation panel left, library right, player fixed at bottom. The library stays visible while editing.
- **Narrow windows/tablets**: Creation and library become two top-level tabs with a persistent compact player.
- **Phones**: Reading and playback are supported; long-form lyric editing may be less comfortable but must remain functional.

## Accessibility Requirements

- Keyboard access for every action and visible focus states.
- Labeled fields and buttons; announce generation status changes to screen readers without excessive chatter.
- Sufficient text and control contrast on the dark theme; do not use color alone for job status or favorites.
- Player controls and seek bar usable without a pointer.

## Out of Scope

- Accounts, cloud sync, payments, credits, social publishing, and Spotify upload.
- Artist voice cloning, reference-audio upload, multiple singers in v1, and training or fine-tuning.
- Section-level regeneration, a multitrack editor, mastering controls, and extensive model-parameter panels.
- A separate general-purpose LLM or Ollama dependency; description-only lyric generation uses ACE-Step's existing LM.

## Open Decisions

- Confirm whether the automatically suggested title should be editable both before and after generation; current recommendation is yes.
- Confirm whether favorites alone are enough for organization or whether folders/playlists become necessary after the library grows.
- Confirm whether `10 songs from one description` means ten independent songs with independently generated lyrics (current interpretation), or ten arrangements of one shared set of generated lyrics.
