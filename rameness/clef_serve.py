"""Clef-Flash behind the Jev / System One HTTP API (POST /v1/systemone), as a drop-in for Kev's server.

Run by `rameness jev up clef` with Clef's own Python environment (never rameness's: it needs PyTorch,
transformers and bitsandbytes), in isolated mode so nothing from this folder shadows its imports:

    python -I clef_serve.py --model Cloudflare/clef-flash --port 8010 --bits 8 [--device cuda]

The model's own release code (joint_schema_model.py, shipped with the weights) does the work; this adds the
HTTP route and 8-bit loading, so the 9B model fits a 16 GB GPU. A Hugging Face repo id is downloaded on the
first start. Requests are handled one at a time.
"""
import argparse
import os
import sys
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="Cloudflare/clef-flash", help="a local folder or a Hugging Face repo id")
ap.add_argument("--port", type=int, default=8010)
ap.add_argument("--bits", type=int, choices=(8, 16), default=8)
ap.add_argument("--device", default="cuda", help="cuda (8-bit needs it) or cpu (16-bit, slow)")
a = ap.parse_args()

path = a.model
if not os.path.isdir(path):                       # a repo id: fetch it once, into the Hugging Face cache
    from huggingface_hub import snapshot_download
    path = snapshot_download(a.model)

import torch  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

sys.path.insert(0, path)
from joint_schema_model import load_release_model, systemone  # noqa: E402

kwargs = {}
if a.bits == 8 and a.device != "cpu":
    from transformers import BitsAndBytesConfig
    kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
t = time.time()
model, processor = load_release_model(path, device=a.device, **kwargs)
where = torch.cuda.get_device_name(0) if a.device != "cpu" else "CPU"
print(f"loaded {a.model} ({a.bits if a.device != 'cpu' else 16}-bit) on {where} in {time.time() - t:.0f}s", flush=True)

app = FastAPI()
lock = threading.Lock()


@app.post("/v1/systemone")
async def route(request: Request):
    body = await request.json()
    with lock:
        t = time.time()
        try:
            out = systemone(model, processor, body)
        except Exception as e:  # a malformed request is the caller's error, not a crash
            return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    out.setdefault("latency_ms", round((time.time() - t) * 1000, 1))
    return out


@app.get("/health")
def health():
    return {"ok": True}


uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
