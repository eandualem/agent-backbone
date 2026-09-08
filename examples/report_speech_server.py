"""Optional loopback Kokoro WAV adapter. See docs/report-audio.md for setup."""

from __future__ import annotations

import argparse
import io


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--voices", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import numpy as np
    import soundfile as sf
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import Response
    from kokoro_onnx import Kokoro
    from pydantic import BaseModel, Field

    engine = Kokoro(args.model, args.voices)
    # Older kokoro-onnx releases pass integer speed to models expecting float32.
    # Match the loaded model input type; leave newer int-style models alone.
    original_run = engine.sess.run
    speed_input = next((item for item in engine.sess.get_inputs() if item.name == "speed"), None)
    if speed_input is not None and speed_input.type == "tensor(float)":

        def run(outputs, inputs, options=None):
            if "speed" in inputs:
                inputs["speed"] = np.asarray(inputs["speed"], dtype=np.float32)
            return original_run(outputs, inputs, options)

        engine.sess.run = run

    class Speech(BaseModel):
        text: str = Field(min_length=1, max_length=400)
        voice: str = "af_heart"
        speed: float = Field(default=1.0, ge=0.5, le=2.0)

    app = FastAPI()

    def speak(request):
        samples, rate = engine.create(
            request.text, voice=request.voice, speed=request.speed, lang="en-us"
        )
        buffer = io.BytesIO()
        sf.write(buffer, samples, rate, format="WAV")
        return Response(buffer.getvalue(), media_type="audio/wav")

    # Assign the local model class directly, avoiding postponed annotation lookup.
    speak.__annotations__ = {"request": Speech}
    app.post("/speak")(speak)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
