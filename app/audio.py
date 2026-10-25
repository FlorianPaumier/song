"""Deterministic two-pass mastering for streaming delivery."""

import asyncio
import json
from pathlib import Path
import re
import shutil


EQ_CHAIN = "highpass=f=28,equalizer=f=95:t=q:w=0.8:g=-1.5"


async def master_for_streaming(source: Path, target: Path) -> Path:
    """Create a 24-bit/48 kHz master at -14 LUFS and -1 dBTP."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg est requis pour le mastering")
    analysis_filter = f"{EQ_CHAIN},loudnorm=I=-14:TP=-1:LRA=11:print_format=json"
    _, stderr = await _run(
        ffmpeg, "-hide_banner", "-nostats", "-i", str(source),
        "-af", analysis_filter, "-f", "null", "-",
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", stderr, re.DOTALL)
    if not match:
        raise RuntimeError("FFmpeg n'a pas retourné les mesures de loudness")
    measured = json.loads(match.group(0))
    final_filter = (
        f"{EQ_CHAIN},loudnorm=I=-14:TP=-1:LRA=11:"
        f"measured_I={measured['input_i']}:measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=summary"
    )
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        await _run(
            ffmpeg, "-y", "-hide_banner", "-nostats", "-i", str(source),
            "-af", final_filter, "-ar", "48000", "-ac", "2",
            "-c:a", "pcm_s24le", "-f", "wav", str(temporary),
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


async def _run(*args: str) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("Le mastering audio a expiré")
    text_out = stdout.decode(errors="replace")
    text_err = stderr.decode(errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg: {text_err[-500:]}")
    return text_out, text_err
