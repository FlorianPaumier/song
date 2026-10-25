"""Single-file worker that submits and tracks ACE-Step jobs in sequence."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.audio import master_for_streaming
from app.config import Settings
from app.long_audio import assemble_long_mix
from app.store import Store


log = logging.getLogger(__name__)

LANGUAGES = {"en": "English", "fr": "French", "de": "German", "es": "Spanish",
             "it": "Italian", "pt": "Portuguese"}
NARRATIVE_LENSES = (
    "a single decisive moment told in chronological order",
    "a personal confession that grows into a collective anthem",
    "a vivid journey from conflict through transformation to resolution",
    "a dialogue with an absent person, with a clear emotional turn",
    "a cinematic scene built from concrete sensory images",
    "a promise tested by escalating obstacles and resolved in the outro",
    "a first-person account whose meaning changes in the final chorus",
    "a symbolic story with one recurring image that evolves each verse",
    "a tense present-tense confrontation followed by release",
    "a memory revisited from a wiser point of view",
)
HOOK_DIRECTIONS = (
    "a short declarative hook", "a call-and-response hook",
    "a rising question-and-answer hook", "a rhythmic three-part hook",
    "a hook built around one evolving image", "a restrained hook that expands on repetition",
    "a hook whose final line changes each time", "a chant-like hook with natural phrasing",
)


def ace_lyrics_brief(job: dict) -> str:
    """Build a strict, per-track brief for ACE-Step's internal 5Hz LM."""
    language = LANGUAGES.get(job["lyric_language"], "English")
    lens = NARRATIVE_LENSES[(int(job["position"]) - 1) % len(NARRATIVE_LENSES)]
    hook = HOOK_DIRECTIONS[int(job["seed"]) % len(HOOK_DIRECTIONS)]
    bpm = f"{job['requested_bpm']} BPM" if job.get("requested_bpm") else "a tempo suited to the style"
    return f"""Create one complete, original song for this specific generation.
Music brief: {job['description']}
Target: {job['requested_duration']} seconds at {bpm}.
Lyrics language: {language} ({job['lyric_language']}). Every sung, spoken, whispered, shouted,
background, ad-lib, and crowd-response word must be exclusively in {language}. Never translate,
code-switch, or insert phrases from another language. English is allowed only inside structural
control tags such as [Intro], [Verse 1], [Pre-Chorus], [Chorus], [Bridge], [Breakdown], and [Outro].

Write coherent lyrics with a clear beginning, development, emotional turn, and ending. Use concrete
imagery, consistent characters and point of view, singable line lengths, and a memorable chorus.
Build enough sections for the requested duration. Do not output explanations, language labels,
Markdown fences, prompt text, or the name/style of a real artist. Do not copy existing lyrics.

Uniqueness direction for batch variation {job['position']}: use {lens}; create {hook}. Avoid generic
filler, disconnected verses, random language changes, repeated verses presented as new material,
and references to being an AI or generating a song. Return a concise original title and the song."""


class Worker:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        self.store.recover()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            while not self._stop.is_set():
                await self._deliver_notifications(client)
                job = self.store.running_job()
                if not job:
                    try:
                        if await self._maintain_ace_if_due(client):
                            continue
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("ACE-Step maintenance failed; generation queue is waiting")
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=30)
                        except TimeoutError:
                            pass
                        continue
                    project = self.store.next_long_project()
                    if project:
                        try:
                            await self.assemble_long_project(project)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.exception("Long project %s failed", project["id"])
                            self.store.update_long_project(project["id"], status="failed", error=str(exc)[:1000])
                        continue
                    if not self.store.paused():
                        job = self.store.next_job()
                if job:
                    try:
                        await self._process(client, job)
                        completed = self.store.get(job["id"])
                        if job["status"] != "completed" and completed and completed["status"] == "completed":
                            count = self.store.record_ace_completion()
                            log.info("ACE-Step completed-song counter: %s/%s", count,
                                     self.settings.ace_maintenance_every)
                    except asyncio.CancelledError:
                        raise
                    except httpx.TransportError as exc:
                        latest = self.store.get(job["id"])
                        if latest and latest["status"] in ("generating", "post_processing"):
                            log.warning("Temporary ACE-Step connection error for %s: %s", job["id"], exc)
                            self.store.update(job["id"], error=f"Connexion temporaire: {exc}"[:1000])
                            try:
                                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
                            except TimeoutError:
                                pass
                        else:
                            log.warning("Preparation/submission failed for %s: %s", job["id"], exc)
                            log.warning("%s", exc)
                            self.store.update(job["id"], status="failed", error=f"Service indisponible: {exc}"[:1000])
                    except Exception as exc:
                        log.exception("Song job %s failed", job["id"])
                        self.store.update(job["id"], status="failed", error=str(exc)[:1000])
                else:
                    cover = self.store.next_cover_job()
                    if cover:
                        try:
                            await self._generate_cover(client, cover)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.exception("Cover job %s failed", cover["id"])
                            self.store.update(cover["id"], cover_status="failed", cover_error=str(exc)[:1000])
                        continue
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=2)
                    except TimeoutError:
                        pass

    async def _maintain_ace_if_due(self, client: httpx.AsyncClient) -> bool:
        threshold = self.settings.ace_maintenance_every
        if threshold <= 0 or self.store.ace_completions_since_maintenance() < threshold:
            return False
        log.info("Refreshing ACE-Step after %s completed songs", threshold)
        response = await client.post(f"{self.settings.ace_url}/v1/reinitialize", timeout=300)
        response.raise_for_status()
        body = response.json()
        if body.get("code", 200) != 200:
            raise RuntimeError(f"ACE-Step reinitialization failed: {body.get('error') or body}")
        response = await client.post(f"{self.settings.ace_url}/v1/init", json={
            "model": self.settings.model,
            "init_llm": True,
            "lm_model_path": self.settings.lm_model,
        }, timeout=900)
        response.raise_for_status()
        body = response.json()
        if body.get("code", 200) != 200:
            raise RuntimeError(f"ACE-Step model reload failed: {body.get('error') or body}")
        response = await client.get(f"{self.settings.ace_url}/health", timeout=30)
        response.raise_for_status()
        self.store.reset_ace_maintenance_counter()
        log.info("ACE-Step memory refresh completed; generation queue can resume")
        return True

    async def assemble_long_project(self, project: dict):
        tracks = self.store.long_project_tracks(project["id"])
        inputs = [Path(track["wav_path"]) for track in tracks if track.get("wav_path")]
        if len(inputs) != len(tracks):
            raise RuntimeError("Un master de segment est manquant")
        folder = self.settings.data_dir / "long"
        target = folder / f"{project['id']}.master.wav"
        await assemble_long_mix(inputs, target, float(project["crossfade"]),
                                int(project["total_duration"]))
        self.store.update_long_project(project["id"], status="completed", wav_path=str(target), error=None)

    async def _generate_cover(self, client: httpx.AsyncClient, job: dict):
        prompt = (f"Square album cover artwork for a song titled {job['title']}. "
                  f"Visual interpretation of: {job['description']}. "
                  "Professional music artwork, striking central composition, coherent color palette, "
                  "high detail, no typography, no words, no letters, no logo, no watermark.")[:1800]
        seed = int(job["seed"])
        workflow = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": self.settings.comfy_unet,
                                                               "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.settings.comfy_clip,
                                                               "type": "lumina2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.settings.comfy_vae}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
            "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
            "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
            "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024,
                                                                        "batch_size": 1}},
            "8": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0,
                  "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0,
                  "model": ["6", 0], "positive": ["4", 0], "negative": ["5", 0],
                  "latent_image": ["7", 0]}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"song-studio/{job['id']}",
                                                               "images": ["9", 0]}},
        }
        self.store.update(job["id"], cover_prompt=prompt, cover_error=None)
        response = await client.post(f"{self.settings.comfy_url}/prompt", json={"prompt": workflow}, timeout=15)
        response.raise_for_status()
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI n'a pas retourné de prompt_id")
        for _ in range(60):
            await asyncio.sleep(30)
            history_response = await client.get(f"{self.settings.comfy_url}/history/{prompt_id}", timeout=15)
            history_response.raise_for_status()
            history = history_response.json().get(prompt_id)
            if not history:
                continue
            status = history.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI a échoué pendant la génération")
            images = history.get("outputs", {}).get("10", {}).get("images", [])
            if images:
                image_info = images[0]
                params = {"filename": image_info["filename"], "subfolder": image_info.get("subfolder", ""),
                          "type": image_info.get("type", "output")}
                image_response = await client.get(f"{self.settings.comfy_url}/view", params=params, timeout=60)
                image_response.raise_for_status()
                folder = self.settings.data_dir / "covers"
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{job['id']}.png"
                temporary = folder / f"{job['id']}.part"
                temporary.write_bytes(image_response.content)
                temporary.replace(target)
                self.store.update(job["id"], cover_status="completed", cover_path=str(target), cover_error=None)
                return
        raise RuntimeError("Délai ComfyUI dépassé")

    async def _deliver_notifications(self, client: httpx.AsyncClient):
        for event in self.store.pending_notifications():
            payload = {"event": "batch.finished", "batch_id": event["batch_id"],
                       "description": event["description"], "total": event["total"],
                       "completed": event["completed"], "failed": event["failed"],
                       "canceled": event["canceled"]}
            try:
                if event["kind"] == "push":
                    message = f"Lot terminé : {event['completed']}/{event['total']} chansons prêtes"
                    if event["failed"] or event["canceled"]:
                        message += f" ({event['failed']} échec(s), {event['canceled']} annulée(s))"
                    response = await client.post(event["url"], content=message.encode(),
                                                 headers={"Title": "Song Studio", "Tags": "musical_note"}, timeout=10)
                else:
                    response = await client.post(event["url"], json=payload,
                                                 headers={"X-Song-Event": "batch.finished",
                                                          "Idempotency-Key": f"song-batch-{event['batch_id']}"}, timeout=10)
                response.raise_for_status()
                self.store.notification_delivered(event["batch_id"], event["kind"])
            except (httpx.HTTPError, ValueError):
                log.exception("Notification failed for batch %s", event["batch_id"])
                self.store.notification_retry(event["batch_id"], event["kind"])

    async def _process(self, client: httpx.AsyncClient, job: dict):
        if job["status"] == "preparing":
            await self._prepare(client, job)
            return
        if job["status"] == "submitting":
            await self._submit(client, job)
            job = self.store.get(job["id"])
        if job["status"] == "post_processing":
            await self._post_process(client, job)
            return
        if job["status"] != "generating":
            return
        await self._poll_generation(client, job)

    async def _prepare(self, client: httpx.AsyncClient, job: dict):
        attempts = int(job.get("preparation_attempts") or 0) + 1
        self.store.update(job["id"], preparation_attempts=attempts, error=None, ace_stage="request")
        lyrics = job["formatted_lyrics"]
        validation = "User-provided lyrics"
        title = job["title"]
        sample_query = ""
        if job["vocal_mode"] == "instrumental":
            lyrics = "[Instrumental]"
            validation = "Instrumental mode"
        elif job["vocal_mode"] == "auto":
            lyrics = ""
            sample_query = ace_lyrics_brief(job)
            validation = ("Generated internally by ACE-Step with fixed language "
                          f"{job['lyric_language']} and per-track uniqueness brief")

        production_parts = [
            "clean controlled low end", "separated kick and bass", "balanced sub frequencies",
            "natural dynamics", "wide detailed stereo image", "polished production",
            "no excessive limiting", "decisive ending",
        ]
        if job["vocal_mode"] != "instrumental":
            production_parts.append("clear intelligible vocals")
        suffix = f" Production: {', '.join(production_parts)}."
        description = job["description"].strip()
        prepared_prompt = f"{description[:max(0, 512 - len(suffix))]}{suffix}"[:512]
        payload = {
            "model": job["model"],
            "lm_model_path": job["lm_model"],
            "thinking": True,
            "prompt": prepared_prompt,
            "lyrics": lyrics,
            "instrumental": job["vocal_mode"] == "instrumental",
            "sample_mode": job["vocal_mode"] == "auto",
            "sample_query": sample_query,
            "use_format": job["vocal_mode"] == "auto",
            "audio_format": "wav",
            "audio_duration": job["requested_duration"],
            "batch_size": 1,
            "vocal_language": job["lyric_language"],
            "use_random_seed": False,
            "seed": job["seed"],
            "inference_steps": 64,
            "guidance_scale": 6.5,
            "shift": 1.0,
            "infer_method": "ode",
            "sampler_mode": "heun",
            "use_adg": False,
            "cfg_interval_start": 0.05,
            "cfg_interval_end": 0.9,
            "lm_temperature": 0.7,
            "lm_cfg_scale": 2.0,
            "lm_top_k": 50,
            "lm_top_p": 0.9,
            "lm_repetition_penalty": 1.05,
            "use_constrained_decoding": True,
            "use_cot_metas": True,
            "use_cot_caption": False,
            "use_cot_language": False,
            "use_cot_lyrics": job["vocal_mode"] == "auto",
            "lm_negative_prompt": job["negative_prompt"],
            "enable_normalization": True,
            "normalization_db": -2.0,
            "fade_in_duration": 0.0,
            "fade_out_duration": 0.0,
            "latent_shift": 0.0,
            "latent_rescale": 1.0,
            "dcw_enabled": True,
            "dcw_mode": "double",
            "dcw_scaler": 0.05,
            "dcw_high_scaler": 0.02,
            "dcw_wavelet": "haar",
            "velocity_norm_threshold": 0.0,
            "velocity_ema_factor": 0.0,
        }
        if job["requested_bpm"] is not None:
            payload["bpm"] = job["requested_bpm"]
        self.store.update(
            job["id"], title=title, formatted_lyrics=lyrics,
            generated_lyrics="",
            prepared_prompt=prepared_prompt, ace_payload=json.dumps(payload, ensure_ascii=False),
            lyrics_validation=validation, status="ready", ace_stage="ready", error=None,
        )

    async def _submit(self, client: httpx.AsyncClient, job: dict):
        if not job.get("ace_payload"):
            self.store.update(job["id"], status="draft")
            return
        try:
            payload = json.loads(job["ace_payload"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Payload ACE-Step sauvegardé invalide") from exc
        self.store.update(job["id"], ace_stage="submitting", error=None)
        attempts = max(1, self.settings.ace_submit_retries)
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(f"{self.settings.ace_url}/release_task", json=payload)
                response.raise_for_status()
                break
            except httpx.TransportError:
                if attempt == attempts:
                    raise
                delay = self.settings.ace_submit_retry_seconds * attempt
                log.warning("ACE-Step unavailable while submitting %s; retry %s/%s in %ss",
                            job["id"], attempt, attempts - 1, delay)
                await asyncio.sleep(delay)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt == attempts:
                    raise
                delay = self.settings.ace_submit_retry_seconds * attempt
                log.warning("ACE-Step returned HTTP %s while submitting %s; retry %s/%s in %ss",
                            exc.response.status_code, job["id"], attempt, attempts - 1, delay)
                await asyncio.sleep(delay)
        data = response.json()
        if data.get("code") != 200 or not data.get("data", {}).get("task_id"):
            raise RuntimeError(f"ACE-Step rejected task: {data.get('error') or data}")
        self.store.update(job["id"], ace_task_id=data["data"]["task_id"], status="generating",
                          ace_stage="queued", error=None)

    async def _poll_generation(self, client: httpx.AsyncClient, job: dict):
        ace_task_id = job["ace_task_id"]
        if not ace_task_id:
            self.store.update(job["id"], status="ready")
            return

        while not self._stop.is_set():
            response = await client.post(f"{self.settings.ace_url}/query_result",
                                         json={"task_id_list": [ace_task_id]})
            response.raise_for_status()
            current = self.store.get(job["id"])
            if (not current or current["status"] != "generating"
                    or current.get("ace_task_id") != ace_task_id):
                log.info("Stopped tracking abandoned ACE-Step task %s", ace_task_id)
                return
            body = response.json()
            entries = body.get("data") or []
            self.store.update(job["id"], last_checked_at=datetime.now(timezone.utc).isoformat())
            if entries:
                entry = entries[0]
                status = entry.get("status", 0)
                raw_result = entry.get("result") or "[]"
                result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
                first = result[0] if result else {}
                progress = first.get("progress")
                self.store.update(job["id"], ace_stage=str(first.get("stage") or "")[:80],
                                  ace_progress=float(progress) if isinstance(progress, (int, float)) else None,
                                  ace_progress_text=str(entry.get("progress_text") or "")[:300])
                if status == 2 or first.get("status") == 2:
                    raise RuntimeError(str(entry.get("progress_text") or first.get("error") or "ACE-Step generation failed")[-1000:])
                if status == 1:
                    audio_url = first.get("file")
                    if not audio_url:
                        raise RuntimeError("ACE-Step reported success without an audio file")
                    relative = urlparse(audio_url)
                    if relative.path != "/v1/audio" or relative.netloc:
                        raise RuntimeError("Unexpected audio URL returned by ACE-Step")
                    self.store.update(job["id"], status="post_processing", result_audio_url=audio_url,
                                      generated_lyrics=job["generated_lyrics"] or first.get("lyrics") or job["formatted_lyrics"],
                                      ace_stage="download", error=None)
                    await self._post_process(client, self.store.get(job["id"]))
                    return
            await asyncio.sleep(self.settings.poll_seconds)

    async def _post_process(self, client: httpx.AsyncClient, job: dict):
        audio_url = job.get("result_audio_url")
        existing_source = Path(job["source_path"]) if job.get("source_path") else None
        if existing_source and existing_source.is_file():
            master = existing_source.with_name(f"{job['id']}.master.wav")
            await master_for_streaming(existing_source, master)
            self.store.update(job["id"], status="completed", wav_path=str(master),
                              ace_stage="completed", ace_progress=1.0, error=None)
            return
        if not audio_url:
            raise RuntimeError("Résultat ACE-Step manquant pour le post-traitement")
        source, master = await self._save_audio(client, job["id"], audio_url)
        self.store.update(job["id"], status="completed", source_path=str(source), wav_path=str(master),
                          ace_stage="completed", ace_progress=1.0, error=None)

    async def _save_audio(self, client: httpx.AsyncClient, track_id: str, audio_url: str) -> tuple[Path, Path]:
        folder = self.settings.data_dir / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"{track_id}.source.wav"
        master = folder / f"{track_id}.master.wav"
        temporary = folder / f"{track_id}.source.part"
        try:
            async with client.stream("GET", f"{self.settings.ace_url}{audio_url}", timeout=300) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
            if temporary.stat().st_size < 1024:
                raise RuntimeError("ACE-Step returned an empty audio file")
            temporary.replace(source)
            self.store.update(track_id, source_path=str(source), ace_stage="mastering")
            await master_for_streaming(source, master)
            return source, master
        finally:
            temporary.unlink(missing_ok=True)
