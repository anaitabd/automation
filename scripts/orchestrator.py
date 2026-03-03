#!/usr/bin/env python3
"""
orchestrator.py — Local pipeline orchestrator + live dashboard for Nexus Cloud.

Replaces AWS Step Functions for Docker-based local development.
Chains 8 Lambda containers sequentially, streaming progress via SSE.

Usage:
    docker compose up orchestrator          # inside Docker
    python scripts/orchestrator.py          # on host (uses localhost:9001-9008)

Dashboard:  http://localhost:3000
API:
    POST /api/run              {niche, profile, dry_run}
    GET  /api/status/<run_id>
    GET  /api/runs
    GET  /api/events/<run_id>  (SSE stream)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
PORT = int(os.environ.get("ORCHESTRATOR_PORT", "3000"))
MODE = os.environ.get("ORCHESTRATOR_MODE", "docker")  # "docker" or "local"
DASHBOARD_PATH = os.environ.get(
    "DASHBOARD_PATH",
    os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html"),
)

# Lambda endpoints: Docker-internal (port 8080) vs host-mapped (9001-9008)
if MODE == "docker":
    _ENDPOINTS = {
        "Research":  "http://nexus-research:8080",
        "Script":    "http://nexus-script:8080",
        "Audio":     "http://nexus-audio:8080",
        "Visuals":   "http://nexus-visuals:8080",
        "Editor":    "http://nexus-editor:8080",
        "Thumbnail": "http://nexus-thumbnail:8080",
        "Upload":    "http://nexus-upload:8080",
        "Notify":    "http://nexus-notify:8080",
    }
else:
    _ENDPOINTS = {
        "Research":  "http://localhost:9001",
        "Script":    "http://localhost:9002",
        "Audio":     "http://localhost:9003",
        "Visuals":   "http://localhost:9004",
        "Editor":    "http://localhost:9005",
        "Thumbnail": "http://localhost:9006",
        "Upload":    "http://localhost:9007",
        "Notify":    "http://localhost:9008",
    }

INVOKE_PATH = "/2015-03-31/functions/function/invocations"
INVOKE_TIMEOUT = 900  # 15 min — matches AWS Lambda max timeout

# Steps that need extended timeouts (seconds)
_STEP_TIMEOUTS: dict[str, int] = {
    "Visuals": 1800,  # 30 min — heavy (downloads + CLIP + FFmpeg), parallelised
    "Editor":  900,    # full 15 min — MediaConvert can be slow
}

# Steps that need extra retries
_STEP_RETRIES: dict[str, int] = {
    "Visuals": 3,     # heavy step — more retries for transient network issues
}

# ── Pipeline Step Definitions ─────────────────────────────────────────────────
# Each step: (name, which keys to send, which keys to extract from response)
PIPELINE = [
    {
        "name": "Research",
        "input_keys": ["run_id", "niche", "profile", "dry_run"],
        "merge_keys": [
            "run_id", "profile", "dry_run", "research_s3_key",
            "selected_topic", "angle", "trending_context",
        ],
    },
    {
        "name": "Script",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "selected_topic", "angle", "trending_context", "research_s3_key",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "title", "section_count", "total_duration_estimate",
        ],
    },
    {
        "name": "Audio",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "title", "total_duration_estimate",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "title", "total_duration_estimate",
            "voiceover_s3_key", "mixed_audio_s3_key",
        ],
    },
    {
        "name": "Visuals",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "total_duration_estimate",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "total_duration_estimate",
            "title", "sections",
        ],
    },
    {
        "name": "Editor",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "sections", "mixed_audio_s3_key",
            "title", "total_duration_estimate",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "title",
            "final_video_s3_key", "video_duration_sec",
        ],
    },
    {
        "name": "Thumbnail",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "final_video_s3_key",
            "title", "video_duration_sec",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "title",
            "final_video_s3_key", "video_duration_sec",
            "thumbnail_s3_keys", "primary_thumbnail_s3_key",
        ],
    },
    {
        "name": "Upload",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "script_s3_key", "final_video_s3_key",
            "primary_thumbnail_s3_key", "title",
            "video_duration_sec", "thumbnail_s3_keys",
        ],
        "merge_keys": [
            "run_id", "profile", "dry_run", "niche",
            "title", "video_id", "video_url",
            "thumbnail_s3_keys", "primary_thumbnail_s3_key",
            "video_duration_sec",
        ],
    },
    {
        "name": "Notify",
        "input_keys": [
            "run_id", "profile", "dry_run", "niche",
            "title", "video_id", "video_url",
            "final_video_s3_key", "video_duration_sec",
            "thumbnail_s3_keys", "primary_thumbnail_s3_key",
        ],
        "merge_keys": [],
    },
]

# ── Run State Store ───────────────────────────────────────────────────────────
_runs: dict[str, dict[str, Any]] = {}
_events: dict[str, list[queue.Queue]] = {}  # run_id -> list of SSE subscriber queues
_lock = threading.Lock()

# Historical step durations for ETA estimation (step_name -> list of durations in seconds)
_step_durations: dict[str, list[float]] = {s["name"]: [] for s in PIPELINE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_step_duration(step_name: str) -> float | None:
    """Return average duration for a step based on history, or a default estimate."""
    # Default estimates (seconds) for first-run when no history exists
    _DEFAULT_ESTIMATES = {
        "Research": 60, "Script": 120, "Audio": 180, "Visuals": 480,
        "Editor": 300, "Thumbnail": 90, "Upload": 60, "Notify": 15,
    }
    history = _step_durations.get(step_name, [])
    if history:
        return sum(history) / len(history)
    return _DEFAULT_ESTIMATES.get(step_name)


def _record_step_duration(step_name: str, duration: float) -> None:
    """Record how long a step took for future ETA estimation."""
    with _lock:
        durations = _step_durations.setdefault(step_name, [])
        durations.append(duration)
        # Keep only last 10 runs for rolling average
        if len(durations) > 10:
            _step_durations[step_name] = durations[-10:]


def _create_run(run_id: str, niche: str, profile: str, dry_run: bool) -> dict:
    run = {
        "run_id": run_id,
        "niche": niche,
        "profile": profile,
        "dry_run": dry_run,
        "status": "RUNNING",
        "current_step": "",
        "current_step_index": 0,
        "total_steps": len(PIPELINE),
        "started_at": _now(),
        "finished_at": None,
        "elapsed_sec": 0,
        "eta_remaining_sec": None,
        "title": "",
        "steps": [],
        "error": None,
    }
    for step in PIPELINE:
        est = _estimate_step_duration(step["name"])
        run["steps"].append({
            "name": step["name"],
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "elapsed_sec": None,
            "estimated_duration_sec": est,
            "error": None,
            "output_summary": None,
        })
    # Calculate total estimated duration
    total_est = sum(s["estimated_duration_sec"] or 0 for s in run["steps"])
    run["eta_remaining_sec"] = total_est if total_est > 0 else None
    with _lock:
        _runs[run_id] = run
        _events[run_id] = []
    return run


def _console_log(event_type: str, data: dict) -> None:
    """Print a coloured one-liner to the terminal for real-time visibility."""
    _COLORS = {
        "step_start":    "\033[1;36m",   # bold cyan
        "step_done":     "\033[1;32m",   # bold green
        "step_error":    "\033[1;31m",   # bold red
        "log":           "\033[0;37m",   # white
        "pipeline_done": "\033[1;35m",   # bold magenta
    }
    RESET = "\033[0m"
    color = _COLORS.get(event_type, "")
    msg = data.get("message", "")
    if not msg:
        status = data.get("status", "")
        msg = f"Pipeline finished — {status}" if status else json.dumps(data, default=str)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}", flush=True)


def _publish(run_id: str, event_type: str, data: dict) -> None:
    """Push an SSE event to all subscribers of a run + print to console."""
    msg = {"type": event_type, "timestamp": _now(), **data}
    _console_log(event_type, data)
    with _lock:
        subscribers = _events.get(run_id, [])
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _subscribe(run_id: str) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=200)
    with _lock:
        _events.setdefault(run_id, []).append(q)
    return q


def _unsubscribe(run_id: str, q: queue.Queue) -> None:
    with _lock:
        subs = _events.get(run_id, [])
        if q in subs:
            subs.remove(q)


# ── Pipeline Execution ────────────────────────────────────────────────────────
# Steps that run in parallel (indices into PIPELINE array)
_PARALLEL_GROUP = {"Audio", "Visuals"}  # runs concurrently after Script


def _wait_for_container(step_name: str, max_wait: int = 60) -> None:
    """Block until the Lambda RIE container is accepting TCP connections."""
    import socket
    base = _ENDPOINTS[step_name]           # e.g. http://nexus-visuals:8080
    host = base.split("://")[1].split(":")[0]
    port = int(base.rsplit(":", 1)[1])
    for attempt in range(max_wait):
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            if attempt % 10 == 0:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\033[0;33m[{ts}] ⏳ Waiting for {step_name} ({host}:{port}) ..."
                      f" attempt {attempt + 1}/{max_wait}\033[0m", flush=True)
            time.sleep(1)
    raise RuntimeError(f"{step_name} container not reachable at {host}:{port} after {max_wait}s")


def _invoke_lambda(step_name: str, payload: dict, retries: int = 2) -> dict:
    """Invoke a Lambda container with readiness check, connect/read timeouts, and retry."""
    _wait_for_container(step_name)

    retries = _STEP_RETRIES.get(step_name, retries)
    url = _ENDPOINTS[step_name] + INVOKE_PATH
    read_timeout = _STEP_TIMEOUTS.get(step_name, INVOKE_TIMEOUT)
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\033[0;90m[{ts}] → POST {url}  (timeout=10/{read_timeout}s, "
              f"attempt {attempt}/{retries})\033[0m", flush=True)
        try:
            resp = requests.post(url, json=payload, timeout=(10, read_timeout))
            print(f"\033[0;90m[{ts}] ← {resp.status_code} from {step_name} "
                  f"({len(resp.content)} bytes)\033[0m", flush=True)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as exc:
            last_err = exc
            print(f"\033[0;31m[{ts}] ⚠ {step_name} attempt {attempt}/{retries} failed: "
                  f"{exc}\033[0m", flush=True)
            if attempt < retries:
                time.sleep(5)
                _wait_for_container(step_name)

    raise RuntimeError(f"{step_name}: {last_err}")


def _exec_step(
    run_id: str, run: dict, state: dict, i: int, step_def: dict,
    pipeline_start: float, total_steps: int, niche: str,
) -> None:
    """Execute a single pipeline step — shared by sequential & parallel paths."""
    step_name = step_def["name"]
    step_info = run["steps"][i]
    step_start_time = time.time()

    remaining_estimated = sum(
        (_estimate_step_duration(PIPELINE[j]["name"]) or 0)
        for j in range(i, total_steps)
    )

    run["current_step"] = step_name
    run["current_step_index"] = i
    pipeline_elapsed = time.time() - pipeline_start
    run["elapsed_sec"] = round(pipeline_elapsed, 1)
    run["eta_remaining_sec"] = round(remaining_estimated, 1)

    step_info["status"] = "running"
    step_info["started_at"] = _now()

    est_duration = _estimate_step_duration(step_name)
    est_str = f" (estimated: {int(est_duration // 60)}m {int(est_duration % 60)}s)" if est_duration else ""

    _publish(run_id, "step_start", {
        "step": step_name,
        "index": i,
        "total_steps": total_steps,
        "message": f"▶ [{i + 1}/{total_steps}] Starting {step_name}...{est_str}",
        "estimated_duration_sec": est_duration,
        "eta_remaining_sec": round(remaining_estimated, 1),
        "pipeline_elapsed_sec": round(pipeline_elapsed, 1),
        "progress_pct": round((i / total_steps) * 100, 1),
    })

    # Build payload: only send keys the step needs (that exist in state)
    payload = {}
    for k in step_def["input_keys"]:
        if k in state:
            payload[k] = state[k]

    _publish(run_id, "log", {
        "step": step_name,
        "message": f"📡 [{i + 1}/{total_steps}] Invoking {step_name} lambda...",
    })

    result = _invoke_lambda(step_name, payload)

    step_elapsed = time.time() - step_start_time

    if isinstance(result, dict) and "errorMessage" in result:
        raise RuntimeError(result["errorMessage"])

    _record_step_duration(step_name, step_elapsed)

    # Merge result keys into cumulative state (thread-safe for parallel)
    with _lock:
        for k in step_def["merge_keys"]:
            if isinstance(result, dict) and k in result:
                state[k] = result[k]
        state["niche"] = niche

    step_info["status"] = "done"
    step_info["finished_at"] = _now()
    step_info["elapsed_sec"] = round(step_elapsed, 1)
    summary = {}
    for k in ["title", "selected_topic", "script_s3_key",
               "final_video_s3_key", "video_url", "video_id"]:
        if k in (result if isinstance(result, dict) else {}):
            summary[k] = result[k]
    step_info["output_summary"] = summary or None

    if "title" in state:
        run["title"] = state["title"]

    pipeline_elapsed = time.time() - pipeline_start
    run["elapsed_sec"] = round(pipeline_elapsed, 1)
    steps_done = sum(1 for s in run["steps"] if s["status"] == "done")
    progress_pct = round((steps_done / total_steps) * 100, 1)

    remaining_eta = sum(
        (_estimate_step_duration(PIPELINE[j]["name"]) or 0)
        for j in range(i + 1, total_steps)
    )
    run["eta_remaining_sec"] = round(remaining_eta, 1)

    elapsed_str = f"{int(step_elapsed // 60)}m {int(step_elapsed % 60)}s"
    eta_str = f"{int(remaining_eta // 60)}m {int(remaining_eta % 60)}s" if remaining_eta > 0 else "almost done"

    _publish(run_id, "step_done", {
        "step": step_name,
        "index": i,
        "total_steps": total_steps,
        "message": f"✅ [{steps_done}/{total_steps}] {step_name} completed in {elapsed_str}",
        "summary": summary,
        "elapsed_sec": round(step_elapsed, 1),
        "pipeline_elapsed_sec": round(pipeline_elapsed, 1),
        "eta_remaining_sec": round(remaining_eta, 1),
        "progress_pct": progress_pct,
    })

    _publish(run_id, "log", {
        "step": step_name,
        "message": f"⏱ {step_name}: {elapsed_str} | "
                   f"Progress: {progress_pct}% | ETA: {eta_str}",
    })


def _run_pipeline(run_id: str, niche: str, profile: str, dry_run: bool) -> None:
    """Execute the pipeline — Audio ∥ Visuals run in parallel."""
    state = {
        "run_id": run_id,
        "niche": niche,
        "profile": profile,
        "dry_run": dry_run,
    }

    run = _runs[run_id]
    run["state"] = state
    pipeline_start = time.time()
    total_steps = len(PIPELINE)

    total_estimated = sum(
        (_estimate_step_duration(s["name"]) or 0) for s in PIPELINE
    )
    # For parallel group, ETA = max(Audio, Visuals) instead of sum
    audio_est = _estimate_step_duration("Audio") or 0
    visuals_est = _estimate_step_duration("Visuals") or 0
    parallel_saving = (audio_est + visuals_est) - max(audio_est, visuals_est)
    total_estimated -= parallel_saving

    _publish(run_id, "log", {
        "step": "Pipeline",
        "message": f"🚀 Pipeline started with {total_steps} steps (Audio ∥ Visuals in parallel). "
                   f"Estimated total: {int(total_estimated // 60)}m {int(total_estimated % 60)}s",
    })

    # Build ordered execution plan:
    #   sequential steps before parallel, parallel group, sequential steps after
    seq_before = []  # (index, step_def)
    parallel = []    # (index, step_def)
    seq_after = []   # (index, step_def)
    past_parallel = False
    for i, step_def in enumerate(PIPELINE):
        if step_def["name"] in _PARALLEL_GROUP:
            parallel.append((i, step_def))
        elif not parallel and not past_parallel:
            seq_before.append((i, step_def))
        else:
            past_parallel = True
            seq_after.append((i, step_def))

    def _fail(step_name: str, i: int, exc: Exception, step_start: float) -> None:
        step_elapsed = time.time() - step_start
        error_msg = str(exc)
        step_info = run["steps"][i]
        step_info["status"] = "error"
        step_info["finished_at"] = _now()
        step_info["elapsed_sec"] = round(step_elapsed, 1)
        step_info["error"] = error_msg
        run["status"] = "FAILED"
        run["error"] = f"{step_name}: {error_msg}"
        run["finished_at"] = _now()
        run["elapsed_sec"] = round(time.time() - pipeline_start, 1)
        run["eta_remaining_sec"] = 0

        _publish(run_id, "step_error", {
            "step": step_name,
            "index": i,
            "total_steps": total_steps,
            "message": f"❌ [{i + 1}/{total_steps}] {step_name} failed after "
                       f"{int(step_elapsed // 60)}m {int(step_elapsed % 60)}s: {error_msg}",
            "error": error_msg,
            "elapsed_sec": round(step_elapsed, 1),
            "pipeline_elapsed_sec": round(time.time() - pipeline_start, 1),
            "progress_pct": round((i / total_steps) * 100, 1),
        })
        _publish(run_id, "pipeline_done", {
            "status": "FAILED",
            "error": f"{step_name}: {error_msg}",
            "total_elapsed_sec": round(time.time() - pipeline_start, 1),
        })

    # ── Phase 1: Sequential steps before parallel (Research, Script) ──
    for i, step_def in seq_before:
        try:
            _exec_step(run_id, run, state, i, step_def, pipeline_start, total_steps, niche)
        except Exception as exc:
            _fail(step_def["name"], i, exc, time.time())
            return

    # ── Phase 2: Parallel group (Audio ∥ Visuals) ──
    if parallel:
        par_names = [sd["name"] for _, sd in parallel]
        _publish(run_id, "log", {
            "step": "Pipeline",
            "message": f"⚡ Running {' ∥ '.join(par_names)} in PARALLEL...",
        })

        errors: list[tuple[str, int, Exception]] = []
        par_start = time.time()

        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {}
            for idx, step_def in parallel:
                fut = pool.submit(
                    _exec_step,
                    run_id, run, state, idx, step_def,
                    pipeline_start, total_steps, niche,
                )
                futures[fut] = (idx, step_def)

            for fut in as_completed(futures):
                idx, step_def = futures[fut]
                exc = fut.exception()
                if exc:
                    errors.append((step_def["name"], idx, exc))

        if errors:
            # Report first error (use par_start for correct elapsed time)
            name, idx, exc = errors[0]
            _fail(name, idx, exc, par_start)
            return

        parallel_elapsed = time.time() - pipeline_start
        _publish(run_id, "log", {
            "step": "Pipeline",
            "message": f"✅ Parallel group finished in "
                       f"{int(parallel_elapsed // 60)}m {int(parallel_elapsed % 60)}s",
        })

    # ── Phase 3: Sequential steps after parallel (Editor, Thumbnail, Upload, Notify) ──
    for i, step_def in seq_after:
        try:
            _exec_step(run_id, run, state, i, step_def, pipeline_start, total_steps, niche)
        except Exception as exc:
            _fail(step_def["name"], i, exc, time.time())
            return

    # All steps completed
    total_elapsed = time.time() - pipeline_start
    run["status"] = "SUCCEEDED"
    run["finished_at"] = _now()
    run["elapsed_sec"] = round(total_elapsed, 1)
    run["eta_remaining_sec"] = 0

    _publish(run_id, "log", {
        "step": "Pipeline",
        "message": f"🎉 Pipeline completed successfully in "
                   f"{int(total_elapsed // 60)}m {int(total_elapsed % 60)}s",
    })
    _publish(run_id, "pipeline_done", {
        "status": "SUCCEEDED",
        "title": state.get("title", ""),
        "total_elapsed_sec": round(total_elapsed, 1),
    })


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        """Compact log format."""
        print(f"[{_now()}] {args[0]}", flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, status: int, data: Any):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # ── Dashboard ──
        if path in ("/", "/index.html"):
            self._serve_dashboard()
            return

        # ── API: health check ──
        if path == "/api/health":
            self._json_response(200, {"status": "healthy", "mode": "local", "port": PORT})
            return

        # ── API: list runs ──
        if path == "/api/runs":
            runs_list = []
            for r in _runs.values():
                runs_list.append({
                    "run_id": r["run_id"],
                    "niche": r["niche"],
                    "profile": r["profile"],
                    "dry_run": r["dry_run"],
                    "status": r["status"],
                    "current_step": r["current_step"],
                    "title": r.get("title", ""),
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                })
            runs_list.sort(key=lambda x: x["started_at"], reverse=True)
            self._json_response(200, runs_list)
            return

        # ── API: run status ──
        if path.startswith("/api/status/"):
            run_id = path.split("/api/status/")[-1]
            run = _runs.get(run_id)
            if not run:
                self._json_response(404, {"error": "run not found"})
                return
            self._json_response(200, run)
            return

        # ── API: run outputs ──
        if path.startswith("/api/outputs/"):
            run_id = path.split("/api/outputs/")[-1]
            run = _runs.get(run_id)
            if not run:
                self._json_response(404, {"error": "run not found"})
                return
            state = run.get("state", {})
            outputs = {
                "run_id": run_id,
                "title": state.get("title", ""),
                "video_url": state.get("video_url"),
                "video_id": state.get("video_id"),
                "final_video_s3_key": state.get("final_video_s3_key"),
                "primary_thumbnail_s3_key": state.get("primary_thumbnail_s3_key"),
                "thumbnail_s3_keys": state.get("thumbnail_s3_keys"),
                "video_duration_sec": state.get("video_duration_sec"),
            }
            self._json_response(200, outputs)
            return

        # ── API: SSE events ──
        if path.startswith("/api/events/"):
            run_id = path.split("/api/events/")[-1]
            if run_id not in _runs:
                self._json_response(404, {"error": "run not found"})
                return
            self._serve_sse(run_id)
            return

        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            niche = body.get("niche", "").strip()
            profile = body.get("profile", "documentary")
            dry_run = bool(body.get("dry_run", False))

            if not niche:
                self._json_response(400, {"error": "niche is required"})
                return
            if profile not in ("documentary", "finance", "entertainment"):
                self._json_response(400, {"error": "invalid profile"})
                return

            run_id = str(uuid.uuid4())
            _create_run(run_id, niche, profile, dry_run)

            thread = threading.Thread(
                target=_run_pipeline,
                args=(run_id, niche, profile, dry_run),
                daemon=True,
            )
            thread.start()

            self._json_response(200, {"run_id": run_id, "status": "RUNNING"})
            return

        self._json_response(404, {"error": "not found"})

    # ── Dashboard serving ──
    def _serve_dashboard(self):
        try:
            dash_path = os.path.abspath(DASHBOARD_PATH)
            with open(dash_path, "r") as f:
                html = f.read()

            # Inject local API base so the React app talks to us
            inject = f"<script>window.__NEXUS_API_BASE__ = 'http://localhost:{PORT}/api';</script>"
            html = html.replace("<head>", f"<head>\n{inject}", 1)

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(html.encode())
        except FileNotFoundError:
            self._json_response(404, {"error": "dashboard/index.html not found"})

    # ── SSE streaming ──
    def _serve_sse(self, run_id: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        q = _subscribe(run_id)
        try:
            # Send current state as first event
            run = _runs.get(run_id)
            if run:
                self._sse_write("snapshot", run)

            last_ping = time.time()
            while True:
                try:
                    msg = q.get(timeout=10)
                    self._sse_write(msg["type"], msg)
                    if msg["type"] == "pipeline_done":
                        break
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

                    # If pipeline already done, close
                    run = _runs.get(run_id, {})
                    if run.get("status") in ("SUCCEEDED", "FAILED"):
                        self._sse_write("pipeline_done", {
                            "status": run["status"],
                            "title": run.get("title", ""),
                        })
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _unsubscribe(run_id, q)

    def _sse_write(self, event_type: str, data: Any):
        payload = json.dumps(data, default=str)
        self.wfile.write(f"event: {event_type}\ndata: {payload}\n\n".encode())
        self.wfile.flush()


# ── Threaded HTTP Server ──────────────────────────────────────────────────────
class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread (needed for SSE)."""
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    print(f"""
╔══════════════════════════════════════════════════════╗
║          NEXUS CLOUD — Local Orchestrator            ║
╠══════════════════════════════════════════════════════╣
║  Dashboard:  http://localhost:{PORT:<24}║
║  Mode:       {MODE:<39}║
╚══════════════════════════════════════════════════════╝
""", flush=True)

    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()

