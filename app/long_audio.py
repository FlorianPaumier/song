"""Assemble mastered instrumental segments into one continuous long-form mix."""

import asyncio
from pathlib import Path
import shutil


async def assemble_long_mix(inputs: list[Path], target: Path, crossfade: float,
                            total_duration: int) -> Path:
    if len(inputs) < 2 or any(not path.is_file() for path in inputs):
        raise RuntimeError("Les segments du mix long sont incomplets")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg est requis pour assembler le mix long")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    labels: list[str] = []
    previous = "0:a"
    for index in range(1, len(inputs)):
        output = f"mix{index}"
        labels.append(
            f"[{previous}][{index}:a]acrossfade=d={crossfade:.3f}:c1=tri:c2=tri[{output}]"
        )
        previous = output
    labels.append(
        f"[{previous}]atrim=duration={total_duration},"
        "alimiter=limit=0.891:attack=5:release=100:level=false[out]"
    )
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        args += ["-i", str(path)]
    args += ["-filter_complex", ";".join(labels), "-map", "[out]", "-map_metadata", "-1",
             "-map_chapters", "-1", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le",
             "-f", "wav", str(temporary)]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=7200)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("L’assemblage du mix long a expiré")
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg: {stderr.decode(errors='replace')[-500:]}")
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
