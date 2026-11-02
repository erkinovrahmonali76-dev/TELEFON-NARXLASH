
import asyncio
from pathlib import Path


async def convert_video(source, output, mode):
    if mode == "round":
        vf = (
            "scale=640:640:force_original_aspect_ratio=increase,"
            "crop=640:640"
        )
    else:
        vf = (
            "scale=540:960:force_original_aspect_ratio=increase,"
            "crop=540:960"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-c:a", "aac",
        "-b:a", "96k",
        "-pix_fmt", "yuv420p",
        "-threads", "2",
        "-movflags", "+faststart",
        str(output)
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    _, error = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            error.decode(errors="ignore")[-700:]
        )

    if not Path(output).exists():
        raise RuntimeError("Video yaratilmadi")

    return output
