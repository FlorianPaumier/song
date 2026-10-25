import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.lyrics import normalize_lyrics
from app.main import BatchCreate, archive_filename, convert_audio, effective_negative_prompt, requested_bpm, retry_stage
from app.config import Settings
from app.audio import master_for_streaming
from app.long_audio import assemble_long_mix
from app.store import Store
from app.worker import Worker, ace_lyrics_brief


def test_config_loads_dotenv_without_overriding_exported_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ACESTEP_API_URL=http://10.0.0.5:8001\n"
        "SONG_MODEL=acestep-v15-xl-turbo\n"
        "SONG_LM_MODEL=acestep-5Hz-lm-1.7B\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["SONG_ENV_FILE"] = str(env_file)
    environment["SONG_MODEL"] = "exported-model"
    environment.pop("ACESTEP_API_URL", None)
    environment.pop("SONG_LM_MODEL", None)

    output = subprocess.check_output([
        str(Path(__file__).parents[1] / ".venv/bin/python"), "-c",
        "from app.config import settings; "
        "print(settings.ace_url, settings.model, settings.lm_model, sep='|')",
    ], cwd=Path(__file__).parents[1], env=environment, text=True).strip()

    assert output == "http://10.0.0.5:8001|exported-model|acestep-5Hz-lm-1.7B"


def test_bpm():
    assert requested_bpm("Fast power metal, 200 BPM") == 200
    assert requested_bpm("slow rock") is None
    with pytest.raises(HTTPException):
        requested_bpm("500 bpm")


def test_lyrics_sections_and_repetition():
    formatted, outline = normalize_lyrics("[Verse 1]\nA line\n[Chorus]\nSing it\n[Chorus]\n[Outro]\nGoodbye")
    assert outline == ["Verse 1", "Chorus", "Chorus", "Outro"]
    assert formatted.count("Sing it") == 2
    formatted, outline = normalize_lyrics("**Intro (spoken):** Hello\n**Chorus:** We stand\n**Chorus (×2)**")
    assert "Hello" in formatted
    assert outline == ["Intro", "Chorus", "Chorus", "Chorus"]


def test_queue_persists_and_searches(tmp_path: Path):
    path = tmp_path / "library.sqlite3"
    store = Store(path)
    batch = store.create_batch("200 BPM power metal", "[Chorus]\nRise", "[Chorus]\nRise", 200,
                               3, 210, "xl", "4b", [11, 22, 33], "Rise")
    assert len(store.tracks("power metal")) == 3
    assert store.track_count("power metal") == 3
    assert len(store.tracks(limit=2, offset=1)) == 2
    assert store.next_job()["position"] == 1
    store.set_paused(True)
    reopened = Store(path)
    assert reopened.paused()
    assert reopened.batch_counts(batch) == {"draft": 2, "preparing": 1}
    reopened.recover()
    assert reopened.batch_counts(batch) == {"draft": 3}
    track = reopened.tracks("Rise")[0]
    reopened.update(track["id"], favorite=1)
    assert len(reopened.tracks(favorite=True)) == 1
    assert reopened.cancel_queued() == 3


def test_ace_maintenance_counter_is_durable(tmp_path: Path):
    path = tmp_path / "library.sqlite3"
    store = Store(path)

    for _ in range(10):
        store.record_ace_completion()

    assert Store(path).ace_completions_since_maintenance() == 10
    store.reset_ace_maintenance_counter()
    assert Store(path).ace_completions_since_maintenance() == 0


def test_ace_maintenance_reinitializes_models_after_ten_completions(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    for _ in range(10):
        store.record_ace_completion()
    worker = Worker(store, Settings(
        ace_url="http://ace.test", data_dir=tmp_path,
        model="acestep-v15-xl-sft", lm_model="acestep-5Hz-lm-1.7B",
        ace_maintenance_every=10,
    ))
    requests = []

    def respond(request: httpx.Request):
        requests.append((request.method, request.url.path,
                         json.loads(request.content) if request.content else None))
        if request.url.path == "/health":
            return httpx.Response(200, json={"data": {"status": "ok"}})
        return httpx.Response(200, json={"code": 200, "data": {}})

    async def maintain():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await worker._maintain_ace_if_due(client) is True

    asyncio.run(maintain())

    assert requests == [
        ("POST", "/v1/reinitialize", None),
        ("POST", "/v1/init", {
            "model": "acestep-v15-xl-sft", "init_llm": True,
            "lm_model_path": "acestep-5Hz-lm-1.7B",
        }),
        ("GET", "/health", None),
    ]
    assert store.ace_completions_since_maintenance() == 0


def test_tracks_api_reports_original_batch_size_for_grouping(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("ambient set", "", "", None, 3, 210, "xl", "4b", [1, 2, 3], "Focus")
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).get("/api/tracks")

    assert response.status_code == 200
    assert [track["batch_size"] for track in response.json()["tracks"]] == [3, 3, 3]


def test_grouped_batch_endpoint_returns_every_track_beyond_page_limit(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    batch_id = store.create_batch(
        "ambient set", "", "", None, 25, 210, "xl", "4b", list(range(1, 26)), "Focus")
    monkeypatch.setattr(main_module, "store", store)

    page = TestClient(main_module.app).get("/api/tracks", params={"limit": 20, "grouped": True}).json()
    group = TestClient(main_module.app).get(f"/api/batches/{batch_id}/tracks")

    assert len(page["tracks"]) == 25
    assert page["pages"] == 1
    assert group.status_code == 200
    assert [track["position"] for track in group.json()["tracks"]] == list(range(1, 26))


def test_batch_details_api_returns_complete_description(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    description = "Long album concept. " * 30
    batch_id = store.create_batch(description, "", "", None, 3, 210, "xl", "4b", [1, 2, 3], "Focus")
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).get(f"/api/batches/{batch_id}")

    assert response.status_code == 200
    assert response.json()["description"] == description


def test_preparation_persists_complete_ace_payload(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("200 BPM power metal", "[Chorus]\nRise", "[Chorus]\nRise", 200,
                       1, 210, "xl", "4b", [11], "Rise", vocal_mode="provided",
                       lyric_language="en", negative_prompt="muddy mix")
    job = store.next_job()
    worker = Worker(store, Settings(data_dir=tmp_path))

    async def prepare():
        async with httpx.AsyncClient() as client:
            await worker._prepare(client, job)

    asyncio.run(prepare())
    saved = store.get(job["id"])
    assert saved["status"] == "ready"
    assert saved["prepared_prompt"].startswith("200 BPM power metal")
    assert "clean controlled low end" in saved["prepared_prompt"]
    assert len(saved["prepared_prompt"]) <= 512
    payload = json.loads(saved["ace_payload"])
    assert payload["bpm"] == 200
    assert payload["lyrics"] == "[Chorus]\nRise"
    assert payload["inference_steps"] == 64
    assert payload["guidance_scale"] == 6.5
    assert payload["shift"] == 1.0
    assert payload["sampler_mode"] == "heun"
    assert payload["normalization_db"] == -2.0
    assert payload["use_cot_metas"] is True


def test_submission_retries_transient_ace_connection_errors(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("rock", "", "", None, 1, 180, "xl", "1.7b", [1], "Rock")
    job = store.next_job()
    store.update(job["id"], ace_payload='{"prompt":"rock"}', status="ready")
    job = store.get(job["id"])
    worker = Worker(store, Settings(data_dir=tmp_path, ace_url="http://ace.test",
                                    ace_submit_retries=3, ace_submit_retry_seconds=0))
    attempts = 0

    def respond(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary outage", request=request)
        return httpx.Response(200, json={"code": 200, "data": {"task_id": "task-123"}})

    async def submit():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await worker._submit(client, job)

    asyncio.run(submit())
    assert attempts == 3
    assert store.get(job["id"])["status"] == "generating"
    assert store.get(job["id"])["ace_task_id"] == "task-123"


def test_instrumental_production_prompt_does_not_request_voice_or_bass_guitar(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    description = "Electronic instrumental with analog synthesizers " * 20
    store.create_batch(description, "", "", None, 1, 180, "xl", "4b", [12], "Synth",
                       vocal_mode="instrumental", lyric_language="en")
    job = store.next_job()
    worker = Worker(store, Settings(data_dir=tmp_path))

    async def prepare():
        async with httpx.AsyncClient() as client:
            await worker._prepare(client, job)

    asyncio.run(prepare())
    saved = store.get(job["id"])
    assert len(saved["prepared_prompt"]) == 512
    assert "vocals" not in saved["prepared_prompt"].lower()
    assert "bass guitar" not in saved["prepared_prompt"].lower()
    assert len(saved["prepared_prompt"].split(" Production:")[0]) > 275


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_streaming_master_is_24_bit_48khz(tmp_path: Path):
    source = tmp_path / "source.wav"
    target = tmp_path / "master.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=80:duration=3", "-c:a", "pcm_s16le", str(source),
    ], check=True)
    asyncio.run(master_for_streaming(source, target))
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,bits_per_sample", "-of", "json", str(target),
    ], text=True)
    stream = json.loads(probe)["streams"][0]
    assert stream["sample_rate"] == "48000"
    assert stream["bits_per_sample"] == 24
    loudness = subprocess.check_output([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(target),
        "-af", "ebur128=peak=true", "-f", "null", "-",
    ], stderr=subprocess.STDOUT, text=True)
    integrated = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", loudness)[-1])
    true_peak = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", loudness)[-1])
    assert -14.2 <= integrated <= -13.8
    assert true_peak <= -1.0


def test_retry_resumes_post_processing_without_regenerating(tmp_path: Path):
    source = tmp_path / "song.source.wav"
    source.write_bytes(b"existing audio")
    assert retry_stage({"source_path": str(source), "result_audio_url": None}) == "post_processing"
    assert retry_stage({"source_path": None, "result_audio_url": "/v1/audio?path=x"}) == "post_processing"
    assert retry_stage({"source_path": None, "result_audio_url": None}) == "generation"


def test_active_track_can_be_force_restarted_from_zero(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("rock", "", "", None, 1, 180, "xl", "1.7b", [17], "Rock")
    track = store.next_job()
    store.update(
        track["id"], status="generating", ace_task_id="old-remote-task",
        result_audio_url="/v1/audio?path=old.wav", prepared_prompt="old prompt",
        ace_payload='{"old": true}', generated_lyrics="old lyrics",
        lyrics_validation="old validation", preparation_attempts=3,
        ace_stage="diffusion", ace_progress=0.6, ace_progress_text="working",
        last_checked_at="2026-09-24T10:00:00+00:00", error="stuck",
    )
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).post(f"/api/tracks/{track['id']}/force-retry")

    assert response.status_code == 200
    restarted = response.json()
    assert restarted["status"] == "draft"
    assert restarted["ace_task_id"] is None
    assert restarted["result_audio_url"] is None
    assert restarted["ace_payload"] is None
    assert restarted["prepared_prompt"] == ""
    assert restarted["generated_lyrics"] == ""
    assert restarted["preparation_attempts"] == 0
    assert restarted["ace_stage"] is None
    assert restarted["ace_progress"] is None
    assert restarted["error"] is None


def test_active_track_can_be_stopped_when_ace_is_unresponsive(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("rock", "", "", None, 1, 180, "xl", "1.7b", [17], "Rock")
    track = store.next_job()
    store.update(track["id"], status="generating", ace_task_id="remote-task", ace_stage="diffusion")
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).post(f"/api/tracks/{track['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert response.json()["ace_task_id"] is None


def test_notification_waits_for_whole_batch_and_is_durable(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    batch = store.create_batch("rock", "", "", None, 2, 180, "xl", "4b", [1, 2], "Rock",
                               "http://example.test/webhook", "http://example.test/topic")
    assert store.pending_notifications() == []
    first = store.next_job()
    store.update(first["id"], status="completed")
    assert store.pending_notifications() == []
    second = store.next_job()
    store.update(second["id"], status="failed")
    events = store.pending_notifications()
    assert {event["kind"] for event in events} == {"webhook", "push"}
    assert events[0]["completed"] == 1
    store.notification_delivered(batch, "webhook")
    assert [event["kind"] for event in Store(tmp_path / "library.sqlite3").pending_notifications()] == ["push"]
    store.notification_retry(batch, "push")
    assert store.pending_notifications() == []


def test_track_progress_fields_persist(tmp_path: Path):
    path = tmp_path / "library.sqlite3"
    store = Store(path)
    store.create_batch("rock", "", "", None, 1, 180, "xl", "4b", [1], "Rock")
    track = store.next_job()
    store.update(track["id"], ace_stage="diffusion", ace_progress=0.5,
                 ace_progress_text="working", last_checked_at="2026-09-20T12:00:00+00:00")
    saved = Store(path).get(track["id"])
    assert saved["ace_stage"] == "diffusion"
    assert saved["ace_progress"] == 0.5
    assert saved["last_checked_at"] == "2026-09-20T12:00:00+00:00"


def test_vocal_mode_is_explicit_and_persisted(tmp_path: Path):
    request = BatchCreate(description="heavy instrumental rock", vocal_mode="instrumental", lyric_language="fr")
    assert request.vocal_mode == "instrumental"
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch(request.description, "", "", None, 1, 180, "xl", "4b", [7], "Track",
                       vocal_mode=request.vocal_mode, lyric_language=request.lyric_language)
    job = store.next_job()
    assert job["vocal_mode"] == "instrumental"
    assert job["lyric_language"] == "fr"


def test_negative_prompt_is_generated_or_overridden():
    instrumental = BatchCreate(description="metal", vocal_mode="instrumental")
    assert "vocals" in effective_negative_prompt(instrumental)
    french = BatchCreate(description="rock", vocal_mode="auto", lyric_language="fr")
    assert "non-French" in effective_negative_prompt(french)
    custom = BatchCreate(description="rock", negative_prompt="no cowbell")
    assert effective_negative_prompt(custom).startswith("no cowbell")
    assert "distorted bass" in effective_negative_prompt(custom)


def test_ace_lyrics_brief_fixes_language_structure_and_varies_by_track():
    base = {"description": "epic power metal", "lyric_language": "fr",
            "requested_duration": 180, "requested_bpm": 200, "seed": 42}
    first = ace_lyrics_brief({**base, "position": 1})
    second = ace_lyrics_brief({**base, "position": 2})
    assert "French (fr)" in first
    assert "exclusively in French" in first
    assert "[Verse 1]" in first and "coherent lyrics" in first
    assert first != second


def test_auto_lyrics_are_delegated_to_ace_step(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("melodic rock", "", "", None, 1, 180, "xl", "4b", [43], "Rock",
                       vocal_mode="auto", lyric_language="de")
    job = store.next_job()
    worker = Worker(store, Settings(data_dir=tmp_path))

    async def prepare():
        async with httpx.AsyncClient() as client:
            await worker._prepare(client, job)

    asyncio.run(prepare())
    saved = store.get(job["id"])
    payload = json.loads(saved["ace_payload"])
    assert payload["lyrics"] == ""
    assert payload["sample_mode"] is True
    assert payload["use_cot_lyrics"] is True
    assert payload["vocal_language"] == "de"
    assert payload["use_cot_language"] is False
    assert "exclusively in German" in payload["sample_query"]
    assert saved["generated_lyrics"] == ""


def test_cover_waits_until_audio_queue_is_finished(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("dark metal", "", "", None, 1, 180, "xl", "4b", [9], "Dark",
                       generate_covers=True)
    assert store.next_cover_job() is None
    audio = store.next_job()
    store.update(audio["id"], status="completed")
    cover = store.next_cover_job()
    assert cover["id"] == audio["id"]
    assert store.get(audio["id"])["cover_status"] == "running"
    assert store.cover_counts() == {"running": 1}


def test_bulk_delete_hides_only_inactive_tracks(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    store.create_batch("rock", "", "", None, 3, 180, "xl", "4b", [1, 2, 3], "Rock")
    active = store.next_job()
    tracks = store.tracks(limit=10)
    inactive_ids = [track["id"] for track in tracks if track["id"] != active["id"]]
    for track_id in inactive_ids:
        store.update(track_id, status="failed")
    assert store.soft_delete_many([active["id"], *inactive_ids]) == 2
    assert store.track_count() == 1
    assert store.get(active["id"])["deleted_at"] is None


def test_archive_filename_is_safe_and_unique():
    used: set[str] = set()
    assert archive_filename('Été / Rock: "Finale"', "mp3", used) == "Ete-Rock-Finale.mp3"
    assert archive_filename('Été / Rock: "Finale"', "mp3", used) == "Ete-Rock-Finale (2).mp3"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_routenote_exports_have_required_audio_properties_and_no_tags(tmp_path: Path):
    source = tmp_path / "master.wav"
    mp3 = tmp_path / "track.routenote.mp3"
    flac = tmp_path / "track.routenote.flac"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=1", "-metadata", "title=must disappear",
        "-c:a", "pcm_s24le", "-ar", "48000", str(source),
    ], check=True)
    asyncio.run(convert_audio(source, mp3, "mp3"))
    asyncio.run(convert_audio(source, flac, "flac"))
    for path, codec in ((mp3, "mp3"), (flac, "flac")):
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_raw_sample:format=bit_rate:format_tags",
            "-of", "json", str(path),
        ], text=True))
        stream = probe["streams"][0]
        assert stream["codec_name"] == codec
        assert stream["sample_rate"] == "44100"
        assert stream["channels"] == 2
        assert not probe["format"].get("tags")
    assert int(json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=bit_rate", "-of", "json", str(mp3)
    ], text=True))["format"]["bit_rate"]) >= 319000
    flac_stream = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=bits_per_raw_sample", "-of", "json", str(flac)
    ], text=True))["streams"][0]
    assert flac_stream["bits_per_raw_sample"] == "16"


def test_long_project_segments_are_hidden_and_become_assemblable(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    project_id = store.create_long_project(
        "Night Drive", "lofi 78 BPM", 1800, 310, 12.0, "stable",
        ["opening", "ending"], 78, "xl", "4b", [1, 2], "vocals")
    assert store.track_count() == 0
    segments = store.long_project_tracks(project_id)
    assert len(segments) == 2
    assert store.next_long_project() is None
    for segment in segments:
        store.update(segment["id"], status="completed", wav_path=str(tmp_path / f"{segment['id']}.wav"))
    claimed = store.next_long_project()
    assert claimed["id"] == project_id
    assert claimed["status"] == "assembling"


def test_completed_long_project_is_assembled_before_next_generation(tmp_path: Path):
    store = Store(tmp_path / "library.sqlite3")
    project_id = store.create_long_project(
        "Night Drive", "lofi 78 BPM", 1800, 310, 12.0, "stable",
        ["opening"], 78, "xl", "4b", [1], "vocals")
    segment = store.long_project_tracks(project_id)[0]
    store.update(segment["id"], status="completed", wav_path=str(tmp_path / "segment.wav"))
    store.create_batch("queued song", "", "", None, 1, 210, "xl", "4b", [2], "Later")

    actions = []

    class ObservedWorker(Worker):
        async def assemble_long_project(self, project):
            actions.append(("assemble", project["id"]))
            self.stop()

        async def _process(self, client, job):
            actions.append(("generate", job["id"]))
            self.stop()

        async def _deliver_notifications(self, client):
            pass

    asyncio.run(ObservedWorker(store, Settings(data_dir=tmp_path)).run())

    assert actions == [("assemble", project_id)]


def test_completed_long_project_can_be_finalized_immediately_through_api(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    project_id = store.create_long_project(
        "Night Drive", "lofi 78 BPM", 1800, 310, 12.0, "stable",
        ["opening"], 78, "xl", "4b", [1], "vocals")
    segment = store.long_project_tracks(project_id)[0]
    store.update(segment["id"], status="completed", wav_path=str(tmp_path / "segment.wav"))
    finalized = []

    class Finalizer:
        async def assemble_long_project(self, project):
            finalized.append(project["id"])

    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "worker", Finalizer())

    response = TestClient(main_module.app).post(f"/api/long-projects/{project_id}/finalize")

    assert response.status_code == 202
    assert response.json() == {"status": "assembling"}
    assert store.get_long_project(project_id)["status"] == "assembling"
    assert finalized == [project_id]


def test_finished_long_project_can_be_removed_without_deleting_its_data(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    project_id = store.create_long_project(
        "Night Drive", "lofi 78 BPM", 1800, 310, 12.0, "stable",
        ["opening"], 78, "xl", "4b", [1], "vocals")
    store.update_long_project(project_id, status="completed", wav_path=str(tmp_path / "long.wav"))
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).delete(f"/api/long-projects/{project_id}")

    assert response.status_code == 204
    assert store.long_projects() == []
    assert store.get_long_project(project_id)["deleted_at"] is not None
    assert len(store.long_project_tracks(project_id)) == 1


def test_active_long_project_cannot_be_removed(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "library.sqlite3")
    project_id = store.create_long_project(
        "Night Drive", "lofi 78 BPM", 1800, 310, 12.0, "stable",
        ["opening"], 78, "xl", "4b", [1], "vocals")
    monkeypatch.setattr(main_module, "store", store)

    response = TestClient(main_module.app).delete(f"/api/long-projects/{project_id}")

    assert response.status_code == 409
    assert len(store.long_projects()) == 1


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_long_mix_has_requested_duration(tmp_path: Path):
    inputs = []
    for frequency in (220, 330):
        path = tmp_path / f"{frequency}.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"sine=frequency={frequency}:duration=1.2", "-ar", "48000", "-ac", "2",
                        "-c:a", "pcm_s24le", str(path)], check=True)
        inputs.append(path)
    target = tmp_path / "long.wav"
    asyncio.run(assemble_long_mix(inputs, target, 0.2, 2))
    duration = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(target)
    ], text=True))
    assert 1.99 <= duration <= 2.01
