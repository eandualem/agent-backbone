"""Optional local Kokoro-compatible speech; no model dependency in Backbone."""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import textwrap
import wave
from pathlib import Path

import httpx

from agent_backbone.config import TelegramConfig, validate_setting
from agent_backbone.models import ProgressReport

_MAX_WAV = 32 * 1024 * 1024


def spoken_report(record: dict) -> str:
    report = ProgressReport.model_validate(record["report"])
    lines = [f"{record['agent_name'] or record['author_name']}. {report.status}."]
    for name, section in report.sections():
        lines.append(f"{name.capitalize()}. {section.text}")
        lines.extend(f"Reference: {link.title}." for link in section.links)
    return "\n".join(lines)


async def generate_voice(record: dict, config: TelegramConfig) -> bytes:
    """Speak every chunk, join PCM, encode a bounded Telegram Ogg/Opus voice."""
    validate_setting("telegram.tts_url", config.tts_url)
    validate_setting("telegram.tts_voice", config.tts_voice)
    encoder = shutil.which("ffmpeg")
    if not encoder:
        raise RuntimeError("ffmpeg is required for optional report audio")
    chunks = textwrap.wrap(spoken_report(record), width=350, break_long_words=True)
    frames: list[bytes] = []
    parameters = None
    size = 0
    async with httpx.AsyncClient(timeout=45, trust_env=False, follow_redirects=False) as client:
        for chunk in chunks:
            wav = bytearray()
            async with client.stream(
                "POST",
                config.tts_url,
                json={"text": chunk, "voice": config.tts_voice, "speed": 1.0},
            ) as response:
                response.raise_for_status()
                async for block in response.aiter_bytes():
                    if size + len(wav) + len(block) > _MAX_WAV:
                        raise ValueError("speech response exceeds audio size limit")
                    wav.extend(block)
            with wave.open(io.BytesIO(wav), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if source.getcomptype() != "NONE" or (parameters and parameters != current):
                    raise ValueError("speech service returned incompatible WAV chunks")
                parameters = current
                pcm = source.readframes(source.getnframes())
                frames.append(pcm)
                size += len(pcm)
    if not parameters or not size:
        raise ValueError("speech service returned no audio")
    with tempfile.TemporaryDirectory(prefix="backbone-report-audio-") as directory:
        wav_path, ogg_path = Path(directory) / "report.wav", Path(directory) / "report.ogg"
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(parameters[0])
            output.setsampwidth(parameters[1])
            output.setframerate(parameters[2])
            output.writeframes(b"".join(frames))
        process = await asyncio.create_subprocess_exec(
            encoder,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(wav_path),
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-y",
            str(ogg_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if (
            process.returncode
            or not ogg_path.is_file()
            or ogg_path.stat().st_size > 8 * 1024 * 1024
        ):
            raise RuntimeError("audio encoding failed or exceeded its size limit")
        return ogg_path.read_bytes()
