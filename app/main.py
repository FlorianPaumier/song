from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import errno
import math
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
import zipfile
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ROOT, settings
from app.audio import master_for_streaming
from app.lyrics import normalize_lyrics
from app.store import Store
from app.worker import Worker


store = Store(settings.data_dir / "library.sqlite3")
worker = Worker(store, settings)
_ace_cache = {"at": 0.0, "value": {"online": False, "model": None, "lm_model": None}}
_ace_cache_lock = asyncio.Lock()
ACTIVE_STATUSES = ("draft", "preparing", "ready", "submitting", "generating", "post_processing")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        worker.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Local Song Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class BatchCreate(BaseModel):
    description: str = Field(min_length=3, max_length=2000)
    lyrics: str = Field(default="", max_length=12000)
    title: str = Field(default="", max_length=120)
    count: int = Field(default=2, ge=1, le=500)
    duration: int = Field(default=210, ge=150, le=240)
    vocal_mode: Literal["instrumental", "auto", "provided"] = "auto"
    lyric_language: Literal["en", "fr", "de", "es", "it", "pt"] = "en"
    negative_prompt: str = Field(default="", max_length=1000)
    generate_covers: bool = False


class TrackEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    favorite: bool | None = None


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class BulkExport(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    format: Literal["mp3", "flac", "both"] = "mp3"


class LongProjectCreate(BaseModel):
    description: str = Field(min_length=3, max_length=2000)
    title: str = Field(default="", max_length=120)
    total_duration: Literal[1800, 3600, 7200] = 3600
    evolution: Literal["stable", "progressive", "energetic"] = "stable"
    negative_prompt: str = Field(default="", max_length=1000)


def requested_bpm(description: str) -> int | None:
    match = re.search(r"\b(\d{2,3})\s*BPM\b", description, re.IGNORECASE)
    if not match:
        return None
    bpm = int(match.group(1))
    if not 30 <= bpm <= 300:
        raise HTTPException(422, "Le BPM demandé doit être entre 30 et 300.")
    return bpm


def suggested_title(description: str, lyrics: str) -> str:
    if lyrics.strip():
        # A concise first sung line is usually a better title than a style description.
        formatted, _ = normalize_lyrics(lyrics)
        for line in formatted.splitlines():
            if line and not line.startswith("["):
                return line.strip(" .!—-\"'")[:60] or "Nouvelle chanson"
    return re.sub(r"\b\d{2,3}\s*BPM\b", "", description, flags=re.IGNORECASE).strip(" ,.-")[:60] or "Nouvelle chanson"


def effective_negative_prompt(request: BatchCreate) -> str:
    quality = ("clipped audio, distorted bass, clipped bass, excessive sub-bass, overdriven low end, "
               "bass pumping, boomy kick, muddy low frequencies, crushed dynamics, harsh digital artifacts, "
               "phase problems, unstable tempo, abrupt ending, low quality")
    if request.negative_prompt.strip():
        return f"{request.negative_prompt.strip()}, {quality}"
    if request.vocal_mode == "instrumental":
        return f"vocals, singing, spoken word, speech, choir, chanting, lyrics, vocalizations, {quality}"
    languages = {"en": "non-English or multilingual lyrics", "fr": "non-French or multilingual lyrics",
                 "de": "non-German or multilingual lyrics", "es": "non-Spanish or multilingual lyrics",
                 "it": "non-Italian or multilingual lyrics", "pt": "non-Portuguese or multilingual lyrics"}
    return f"{languages[request.lyric_language]}, unintelligible vocals, incorrect pronunciation, {quality}"


def long_segment_prompt(description: str, evolution: str, position: int, count: int) -> str:
    progress = (position - 1) / max(1, count - 1)
    if position == 1:
        phase = "opening section, establish the core groove gradually, transition-friendly ending"
    elif position == count:
        phase = "final section, satisfying controlled resolution and a clean conclusive ending"
    elif evolution == "progressive":
        phase = ("develop the established palette with a gradual increase in density and intensity"
                 if progress < 0.7 else "late climax, fullest arrangement, then prepare the final resolution")
    elif evolution == "energetic":
        phase = ("energetic peak with a strong groove" if position % 3 == 0
                 else "contrast section with reduced density before the next energetic rise")
    else:
        phase = "steady continuous mood with subtle melodic and textural variation"
    return (f"{description}. Instrumental long-form mix segment {position} of {count}: {phase}. "
            "Preserve the same genre, tempo, tonal center, drum character, bass timbre and production "
            "palette across the complete project. Introduce original variation without vocals, speech, "
            "chants or abrupt stylistic changes. Start and end with an active transition-compatible groove.")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    queue = store.counts()
    covers = store.cover_counts()
    has_work = any(queue.get(name) for name in ACTIVE_STATUSES) or bool(
        covers.get("queued") or covers.get("running"))
    async with _ace_cache_lock:
        if has_work and time.monotonic() - _ace_cache["at"] >= 60:
            ace = {"online": False, "model": None, "lm_model": None}
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(f"{settings.ace_url}/health")
                    response.raise_for_status()
                    data = response.json().get("data", {})
                    ace = {"online": True, "model": data.get("loaded_model"),
                           "lm_model": data.get("loaded_lm_model")}
            except (httpx.HTTPError, ValueError):
                pass
            _ace_cache.update(at=time.monotonic(), value=ace)
        ace = _ace_cache["value"]
    return {"ace": ace, "queue": queue, "covers": covers, "idle": not has_work,
            "paused": store.paused(),
            "configured_model": settings.model, "configured_lm_model": settings.lm_model,
            "features": ["long_projects", "routenote_exports", "bulk_download"]}


@app.post("/api/preview")
async def preview(request: BatchCreate):
    _, outline = normalize_lyrics(request.lyrics)
    return {"bpm": requested_bpm(request.description), "outline": outline,
            "title": request.title.strip() or suggested_title(request.description, request.lyrics),
            "vocal_mode": request.vocal_mode,
            "lyrics_generated": request.vocal_mode == "auto",
            "negative_prompt": effective_negative_prompt(request)}


@app.post("/api/batches", status_code=201)
async def create_batch(request: BatchCreate):
    description = request.description.strip()
    if not description:
        raise HTTPException(422, "La description est obligatoire.")
    if request.vocal_mode == "provided" and not request.lyrics.strip():
        raise HTTPException(422, "Ajoute des paroles ou choisis Instrumental / Paroles automatiques.")
    formatted, outline = normalize_lyrics(request.lyrics)
    bpm = requested_bpm(description)
    title = request.title.strip() or suggested_title(description, request.lyrics)
    seeds = [secrets.randbelow(2**32 - 1) + 1 for _ in range(request.count)]
    batch_id = store.create_batch(description, request.lyrics, formatted, bpm, request.count,
                                  request.duration, settings.model, settings.lm_model, seeds, title,
                                  settings.webhook_url, settings.push_url, request.vocal_mode,
                                  request.lyric_language, effective_negative_prompt(request),
                                  request.generate_covers)
    return {"batch_id": batch_id, "count": request.count, "bpm": bpm, "outline": outline}


@app.post("/api/long-projects", status_code=201)
async def create_long_project(request: LongProjectCreate):
    description = request.description.strip()
    bpm = requested_bpm(description)
    count = math.ceil(request.total_duration / 300)
    crossfade = 12.0 if bpm is None else max(8.0, min(24.0, 8 * 4 * 60 / bpm))
    segment_duration = math.ceil((request.total_duration + crossfade * (count - 1)) / count)
    prompts = [long_segment_prompt(description, request.evolution, position, count)
               for position in range(1, count + 1)]
    seeds = [secrets.randbelow(2**32 - 1) + 1 for _ in range(count)]
    title = request.title.strip() or suggested_title(description, "")
    negative = effective_negative_prompt(BatchCreate(
        description=description, vocal_mode="instrumental", negative_prompt=request.negative_prompt))
    project_id = store.create_long_project(
        title, description, request.total_duration, segment_duration, crossfade,
        request.evolution, prompts, bpm, settings.model, settings.lm_model, seeds, negative)
    return {"project_id": project_id, "segments": count, "segment_duration": segment_duration,
            "crossfade": crossfade, "total_duration": request.total_duration}


@app.get("/api/long-projects")
async def list_long_projects():
    return {"projects": store.long_projects()}


@app.delete("/api/long-projects/{project_id}", status_code=204)
async def delete_long_project(project_id: str):
    project = store.get_long_project(project_id)
    if not project or project.get("deleted_at"):
        raise HTTPException(404, "Projet long introuvable")
    if not store.soft_delete_long_project(project_id):
        raise HTTPException(409, "Un projet long en cours ne peut pas être retiré")


@app.post("/api/long-projects/{project_id}/retry")
async def retry_long_project(project_id: str):
    if not store.get_long_project(project_id):
        raise HTTPException(404, "Projet long introuvable")
    retried = store.retry_long_project(project_id)
    if not retried:
        raise HTTPException(409, "Aucun segment en échec à relancer")
    return {"retried": retried}


async def finalize_long_project_task(project: dict):
    try:
        await worker.assemble_long_project(project)
    except Exception as exc:
        store.update_long_project(project["id"], status="failed", error=str(exc)[:1000])


@app.post("/api/long-projects/{project_id}/finalize", status_code=202)
async def finalize_long_project(project_id: str, background_tasks: BackgroundTasks):
    if not store.get_long_project(project_id):
        raise HTTPException(404, "Projet long introuvable")
    project = store.claim_long_project(project_id)
    if not project:
        raise HTTPException(409, "Tous les segments doivent être terminés avant la finalisation")
    background_tasks.add_task(finalize_long_project_task, project)
    return {"status": "assembling"}


@app.get("/api/tracks")
async def list_tracks(q: str = Query(default="", max_length=200), favorite: bool = False,
                      grouped: bool = False, limit: int = Query(default=10, ge=1, le=100),
                      page: int = Query(default=1, ge=1)):
    query = q.strip()
    total = store.track_count(query, favorite)
    if grouped:
        tracks, unit_total = store.grouped_tracks(query, favorite, limit, (page - 1) * limit)
        pages = max(1, (unit_total + limit - 1) // limit)
    else:
        tracks = store.tracks(query, favorite, limit=limit, offset=(page - 1) * limit)
        pages = max(1, (total + limit - 1) // limit)
    return {"tracks": tracks, "total": total, "page": page, "pages": pages}


@app.get("/api/batches/{batch_id}/tracks")
async def batch_tracks(batch_id: str):
    tracks = store.batch_tracks(batch_id)
    if not tracks:
        raise HTTPException(404, "Lot introuvable")
    return {"tracks": tracks}


@app.get("/api/batches/{batch_id}")
async def batch_status(batch_id: str):
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Lot introuvable")
    return {"batch_id": batch_id, "description": batch["description"],
            "created_at": batch["created_at"], "counts": store.batch_counts(batch_id)}


@app.get("/api/finished-batches")
async def finished_batches():
    return {"batches": store.finished_batches()}


@app.patch("/api/tracks/{track_id}")
async def edit_track(track_id: str, request: TrackEdit):
    if not store.get(track_id):
        raise HTTPException(404, "Chanson introuvable")
    changes = request.model_dump(exclude_none=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
    if "favorite" in changes:
        changes["favorite"] = int(changes["favorite"])
    if changes:
        store.update(track_id, **changes)
    return store.get(track_id)


@app.delete("/api/tracks/{track_id}", status_code=204)
async def delete_track(track_id: str):
    track = store.get(track_id)
    if not track or track.get("deleted_at"):
        raise HTTPException(404, "Chanson introuvable")
    if not store.soft_delete(track_id):
        raise HTTPException(409, "Une chanson ou sa pochette en cours ne peut pas être supprimée")


@app.post("/api/tracks/delete-bulk")
async def delete_tracks(request: BulkDelete):
    return {"deleted": store.soft_delete_many(list(dict.fromkeys(request.ids)))}


@app.post("/api/tracks/{track_id}/retry")
async def retry_track(track_id: str):
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    if track["status"] not in ("failed", "canceled"):
        raise HTTPException(409, "Seule une tâche échouée ou annulée peut être relancée")
    if retry_stage(track) == "post_processing":
        store.update(track_id, status="post_processing", error=None, ace_stage="mastering",
                     ace_progress_text=None, last_checked_at=None)
    else:
        next_status = "ready" if track.get("ace_payload") else "draft"
        store.update(track_id, status=next_status, ace_task_id=None, result_audio_url=None, error=None,
                     ace_stage=None, ace_progress=None, ace_progress_text=None, last_checked_at=None)
    return store.get(track_id)


@app.post("/api/tracks/{track_id}/force-retry")
async def force_retry_track(track_id: str):
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    if track["status"] not in ("preparing", "ready", "submitting", "generating", "post_processing"):
        raise HTTPException(409, "Seule une tâche active peut être redémarrée de force")
    store.update(
        track_id, status="draft", ace_task_id=None, result_audio_url=None,
        source_path=None, wav_path=None, mp3_path=None, error=None,
        generated_lyrics="", prepared_prompt="", ace_payload=None,
        lyrics_validation=None, preparation_attempts=0, ace_stage=None,
        ace_progress=None, ace_progress_text=None, last_checked_at=None,
    )
    return store.get(track_id)


@app.post("/api/tracks/{track_id}/cancel")
async def cancel_track(track_id: str):
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    if track["status"] not in ("preparing", "ready", "submitting", "generating", "post_processing"):
        raise HTTPException(409, "Seule une tâche active peut être arrêtée")
    store.update(track_id, status="canceled", ace_task_id=None, result_audio_url=None,
                 error="Arrêtée par l’utilisateur", ace_stage="canceled",
                 ace_progress_text=None, last_checked_at=None)
    return store.get(track_id)


def retry_stage(track: dict) -> str:
    source = Path(track["source_path"]) if track.get("source_path") else None
    if (source and source.is_file()) or track.get("result_audio_url"):
        return "post_processing"
    return "generation"


@app.post("/api/tracks/{track_id}/cover/retry")
async def retry_cover(track_id: str):
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    if track["status"] != "completed":
        raise HTTPException(409, "La chanson doit être terminée avant sa pochette")
    store.update(track_id, cover_status="queued", cover_error=None)
    return store.get(track_id)


@app.get("/api/tracks/{track_id}/cover")
async def cover(track_id: str, download: bool = False):
    track = store.get(track_id)
    source = Path(track["cover_path"]) if track and track["cover_path"] else None
    if not source or not source.is_file():
        raise HTTPException(404, "Pochette indisponible")
    if not download:
        return FileResponse(source, media_type="image/png")
    from PIL import Image
    target = source.with_suffix(".3000.jpg")
    if not target.is_file():
        with Image.open(source) as image:
            image.convert("RGB").resize((3000, 3000), Image.Resampling.LANCZOS).save(target, "JPEG", quality=95)
    return FileResponse(target, media_type="image/jpeg", filename=f"{track['title']}-cover.jpg")


@app.post("/api/queue/pause")
async def pause_queue():
    store.set_paused(True)
    return {"paused": True}


@app.post("/api/queue/resume")
async def resume_queue():
    store.set_paused(False)
    return {"paused": False}


@app.post("/api/queue/cancel-pending")
async def cancel_pending():
    return {"canceled": store.cancel_queued()}


def finished_track(track_id: str) -> tuple[dict, Path]:
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    source = Path(track["wav_path"]) if track["wav_path"] else None
    if track["status"] != "completed" or not source or not source.is_file():
        raise HTTPException(404, "Fichier audio indisponible")
    return track, source


async def restore_track_audio(track: dict) -> Path | None:
    """Restore deleted local audio from ACE-Step's retained result URL."""
    audio_url = track.get("result_audio_url")
    if not audio_url:
        return None
    relative = urlparse(audio_url)
    if relative.path != "/v1/audio" or relative.netloc:
        return None
    folder = settings.data_dir / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(track.get("source_path") or folder / f"{track['id']}.source.wav")
    master = Path(track.get("wav_path") or folder / f"{track['id']}.master.wav")
    temporary = source.with_suffix(".source.part")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", f"{settings.ace_url}{audio_url}") as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
        if temporary.stat().st_size < 1024:
            raise ValueError("ACE-Step returned an empty audio file")
        temporary.replace(source)
        await master_for_streaming(source, master)
        store.update(track["id"], source_path=str(source), wav_path=str(master),
                     ace_stage="completed", error=None)
        return master
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise HTTPException(507, "Espace disque insuffisant pour restaurer le fichier audio") from exc
        raise HTTPException(404, f"Impossible de restaurer le fichier audio: {exc}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(404, f"Le fichier ACE-Step n’est plus disponible: {exc}") from exc


@app.get("/api/tracks/{track_id}/audio")
async def play_audio(track_id: str):
    track = store.get(track_id)
    if not track:
        raise HTTPException(404, "Chanson introuvable")
    source = Path(track["wav_path"]) if track.get("wav_path") else None
    if track["status"] != "completed":
        raise HTTPException(404, "Fichier audio indisponible")
    if not source or not source.is_file():
        source = await restore_track_audio(track)
    if not source or not source.is_file():
        raise HTTPException(404, "Fichier audio indisponible")
    return FileResponse(source, media_type="audio/wav")


async def convert_audio(source: Path, target: Path, format: Literal["master", "mp3", "flac"]):
    if not shutil.which("ffmpeg"):
        raise HTTPException(503, "FFmpeg est requis pour l'export")
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn"]
    if format == "mp3":
        args += ["-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "320k",
                 "-id3v2_version", "0", "-write_id3v1", "0", str(target)]
    elif format == "flac":
        args += ["-ar", "44100", "-ac", "2", "-c:a", "flac", "-sample_fmt", "s16",
                 "-metadata", "encoder=", str(target)]
    else:
        args += ["-af", "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "48000", "-ac", "2",
                 "-c:a", "pcm_s24le", str(target)]
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except TimeoutError:
        process.kill()
        await process.communicate()
        target.unlink(missing_ok=True)
        raise HTTPException(504, "L'export audio a expiré")
    if process.returncode != 0:
        target.unlink(missing_ok=True)
        raise HTTPException(500, f"FFmpeg: {stderr.decode(errors='replace')[-300:]}")
    if format == "flac":
        strip_flac_metadata(target)


def strip_flac_metadata(path: Path) -> None:
    """Remove Vorbis comments and embedded pictures while preserving FLAC audio frames."""
    temporary = path.with_suffix(path.suffix + ".clean")
    try:
        with path.open("rb") as source:
            if source.read(4) != b"fLaC":
                raise HTTPException(500, "Le fichier FLAC généré est invalide")
            retained: list[tuple[int, bytes]] = []
            while True:
                header = source.read(4)
                if len(header) != 4:
                    raise HTTPException(500, "Les métadonnées FLAC sont incomplètes")
                is_last = bool(header[0] & 0x80)
                block_type = header[0] & 0x7F
                length = int.from_bytes(header[1:4], "big")
                data = source.read(length)
                if len(data) != length:
                    raise HTTPException(500, "Les métadonnées FLAC sont tronquées")
                if block_type not in (4, 6):  # Vorbis comment and picture blocks
                    retained.append((block_type, data))
                if is_last:
                    break
            if not retained or retained[0][0] != 0:
                raise HTTPException(500, "Le bloc STREAMINFO du FLAC est absent")
            with temporary.open("wb") as output:
                output.write(b"fLaC")
                for index, (block_type, data) in enumerate(retained):
                    flag = 0x80 if index == len(retained) - 1 else 0
                    output.write(bytes([flag | block_type]))
                    output.write(len(data).to_bytes(3, "big"))
                    output.write(data)
                shutil.copyfileobj(source, output)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def finished_long_project(project_id: str) -> tuple[dict, Path]:
    project = store.get_long_project(project_id)
    source = Path(project["wav_path"]) if project and project.get("wav_path") else None
    if not project or project["status"] != "completed" or not source or not source.is_file():
        raise HTTPException(404, "Mix long indisponible")
    return project, source


@app.get("/api/long-projects/{project_id}/audio")
async def play_long_project(project_id: str):
    _, source = finished_long_project(project_id)
    return FileResponse(source, media_type="audio/wav")


@app.get("/api/long-projects/{project_id}/export")
async def export_long_project(project_id: str, format: str = Query(pattern="^(flac|mp3)$")):
    project, source = finished_long_project(project_id)
    target = source.with_suffix(f".routenote.{format}")
    if not target.is_file():
        await convert_audio(source, target, format)
        store.update_long_project(project_id, **{f"{format}_path": str(target)})
    media_type = "audio/flac" if format == "flac" else "audio/mpeg"
    return FileResponse(target, media_type=media_type,
                        filename=archive_filename(project["title"], format, set()))


async def export_path(track_id: str, format: Literal["mp3", "flac"]) -> tuple[dict, Path]:
    track = store.get(track_id)
    if not track or track["status"] != "completed":
        raise HTTPException(404, "Fichier audio indisponible")
    source = Path(track["wav_path"]) if track.get("wav_path") else None
    if not source or not source.is_file():
        source = await restore_track_audio(track)
    if not source or not source.is_file():
        raise HTTPException(404, "Fichier audio indisponible")
    master = source.with_name(f"{track_id}.master.wav")
    if not master.is_file():
        await convert_audio(source, master, "master")
    target = source.with_suffix(f".routenote.{format}")
    if not target.is_file():
        await convert_audio(master, target, format)
        if format == "mp3":
            store.update(track_id, mp3_path=str(target))
    return track, target


def archive_filename(title: str, extension: str, used: set[str]) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-")[:80] or "track"
    candidate = f"{base}.{extension}"
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base} ({suffix}).{extension}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


@app.get("/api/tracks/{track_id}/export")
async def export_track(track_id: str, format: str = Query(pattern="^(flac|mp3)$")):
    track, path = await export_path(track_id, format)
    media_type = "audio/flac" if format == "flac" else "audio/mpeg"
    return FileResponse(path, media_type=media_type,
                        filename=archive_filename(track["title"], format, set()))


@app.post("/api/tracks/export-bulk")
async def export_tracks(request: BulkExport, background_tasks: BackgroundTasks):
    ids = list(dict.fromkeys(request.ids))
    formats = ("mp3", "flac") if request.format == "both" else (request.format,)
    files: list[tuple[dict, Path, str]] = []
    for track_id in ids:
        try:
            for audio_format in formats:
                track, path = await export_path(track_id, audio_format)
                files.append((track, path, audio_format))
        except HTTPException:
            raise
    if not files:
        raise HTTPException(422, "Aucun morceau terminé dans la sélection")
    archive_dir = settings.data_dir / "exports"
    archive_dir.mkdir(parents=True, exist_ok=True)
    handle, archive_name = tempfile.mkstemp(prefix="song-studio-", suffix=".zip", dir=archive_dir)
    os.close(handle)
    archive = Path(archive_name)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for track, path, audio_format in files:
                bundle.write(path, archive_filename(track["title"], audio_format, used))
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    background_tasks.add_task(archive.unlink, missing_ok=True)
    return FileResponse(archive, media_type="application/zip", filename="song-studio-export.zip",
                        background=background_tasks)
