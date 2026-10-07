# Kev review integration — prepared, not enabled

The adapter supports a runner-local Kev `/v1/systemone` service. It asks five typed binary questions covering useful behavior, public reusability, implementation fidelity, normal/edge tests and declared permissions. Every score must be at least 0.95; this initial threshold needs calibration against real accepted, rejected and adversarial SOPs. It is not a measured security probability. Missing/invalid answers or service outages block merging. There is no lexical fallback. Security rejection still uses Gitleaks, deterministic checks and the separately configured evidence-based security model.

The workflow exposes `SOP_REVIEW_BACKEND`, `SOP_KEV_MODEL` and `SOP_KEV_REVISION`; the latter identifies the deployed checkpoint in approval-cache policy. These variables do not install or start Kev. **Do not enable the backend until a trusted startup step and evaluation are in place.** The default remains GitHub Models. The service must run on the same runner at `http://127.0.0.1:8008/v1/systemone`. Do not expose your workstation server publicly. Never use a persistent privileged self-hosted runner to execute submitted SOP code.

Known local pins:
- Kev source: `eb45fd2381396eb7edc3964b753ebc1b0ab1da2b`.
- Kev-4B checkpoint: `6cfce5c2fa4b4bd64026336ab649c5ca78857d52`.

Deployment preparation: install pinned Kev with CPU-only PyTorch in an isolated environment, pin dependencies and the checkpoint, set `CUDA_VISIBLE_DEVICES=''` and `KEV_DTYPE=bf16`, start `python -m kev.serve --run jaredpalmer/kev-4b@6cfce5c2fa4b4bd64026336ab649c5ca78857d52 --port 8008`, and wait for readiness. Verify the server does not truncate SOP input silently; refuse over-budget submissions. Benchmark memory, disk, latency and scores before choosing the runner. Local CPU memory was roughly 10 GB; the standard 16 GB public GitHub runner has limited headroom, and cold model/dependency downloads need additional disk and timeout budget. A larger ephemeral runner or a lighter checkpoint are alternatives requiring separate validation.

After evaluations pass, set `SOP_REVIEW_BACKEND=kev`, `SOP_KEV_MODEL=kev` and the pinned revision. Include the trusted installation/startup code and model/dependency pins in the approval policy hash. The current adapter hashes its own policy, backend and revision variables, but cannot verify those variables match actual server weights. That verification is a deployment requirement, not an implemented guarantee.
