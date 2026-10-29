"""Baby video renderer for n8n. Generates a 15-second MP4 with narration."""

import os
import re
import secrets
import subprocess
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Baby Video Renderer", version="1.0.0")
OUTPUT_DIR = Path("/app/output")
API_TOKEN = os.environ.get("RENDERER_API_TOKEN", "")


class VideoRequest(BaseModel):
    job_id: str = Field(default="test", min_length=1, max_length=80)


def run_command(args: list[str], timeout: int = 300) -> None:
    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Rendering timed out") from exc
    except subprocess.CalledProcessError as exc:
        # Limit diagnostic output so API responses do not contain huge FFmpeg logs.
        error = (exc.stderr or "Unknown FFmpeg error")[-2500:]
        raise HTTPException(status_code=500, detail=f"Rendering failed: {error}") from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/generate-video")
def generate_video(
    request: VideoRequest,
    x_api_key: str = Header(default=""),
) -> dict:
    if not API_TOKEN or not secrets.compare_digest(x_api_key, API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Current n8n workflow writes all files to a shared output directory.
    # Run only one rendering job at a time until job-specific folders are added.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", request.job_id):
        raise HTTPException(status_code=422, detail="Invalid job_id")

    images = [OUTPUT_DIR / f"scene_{i:02d}.png" for i in range(1, 4)]
    narration = OUTPUT_DIR / "narration.mp3"
    output = OUTPUT_DIR / "final.mp4"
    temporary = OUTPUT_DIR / "final_rendering.mp4"

    for source in [*images, narration]:
        if not source.is_file() or source.stat().st_size == 0:
            raise HTTPException(status_code=404, detail=f"Missing or empty file: {source.name}")

    # Each image contributes exactly 5 seconds at 30 fps.
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for image in images:
        command += ["-loop", "1", "-framerate", "30", "-t", "5", "-i", str(image)]
    command += ["-i", str(narration)]

    filters = []
    for i in range(3):
        filters.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,format=yuv420p[v{i}]"
        )
    filters.append("[v0][v1][v2]concat=n=3:v=1:a=0[v]")

    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
        "-map", "3:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-af", "apad",
        "-t", "15",
        "-movflags", "+faststart",
        str(temporary),
    ]

    try:
        run_command(command)
        # Decode every frame and audio packet to detect damaged output.
        run_command([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-i", str(temporary), "-f", "null", "-",
        ])
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "status": "completed",
        "job_id": request.job_id,
        "video": str(output),
        "duration_seconds": 15,
        "has_audio": True,
    }
