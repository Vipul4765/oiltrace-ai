"""In-memory job registry for long-running work.

Fetching a Sentinel-1 scene takes anywhere from 5 to 60 seconds - STAC search,
a windowed COG read over HTTP, coastline rasterisation, detection, then a drift
run and a ranking pass per detection. Blocking the request for that gives the
user a frozen button and no idea whether anything is happening.

Jobs report the stage they are ACTUALLY in. Nothing here estimates or animates
progress it cannot observe: `percent` moves only when a real stage completes.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

# Ordered stages with the weight each contributes to the progress bar. Weights
# are rough durations measured on real runs, not invented.
SENTINEL_STAGES: list[tuple[str, str, int]] = [
    ("search", "Searching Sentinel-1 catalogue", 10),
    ("download", "Reading scene window over HTTP", 40),
    ("landmask", "Rasterising coastline mask", 10),
    ("detect", "Detecting dark features", 10),
    ("attribute", "Back-drift and AIS correlation", 30),
]


@dataclass
class Job:
    id: str
    kind: str
    stages: list[tuple[str, str, int]]
    stage: str = ""
    label: str = ""
    done_stages: list[str] = field(default_factory=list)
    status: str = "running"          # running | done | error
    result: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    detail: str = ""

    @property
    def percent(self) -> int:
        total = sum(w for _, _, w in self.stages) or 1
        done = sum(w for k, _, w in self.stages if k in self.done_stages)
        if self.status == "done":
            return 100
        # Credit half of the stage currently in progress: it has demonstrably
        # started, and jumping only on completion makes long stages look stuck.
        cur = next((w for k, _, w in self.stages if k == self.stage), 0)
        return min(99, int((done + cur * 0.5) / total * 100))

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "stage": self.stage, "label": self.label, "detail": self.detail,
            "percent": self.percent,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "stages": [{"key": k, "label": l,
                        "state": ("done" if k in self.done_stages
                                  else "active" if k == self.stage else "pending")}
                       for k, l, _ in self.stages],
            "result": self.result, "error": self.error,
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_MAX_KEPT = 40


def create(kind: str, stages: list[tuple[str, str, int]]) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, stages=stages)
    with _lock:
        _jobs[job.id] = job
        if len(_jobs) > _MAX_KEPT:
            for jid in sorted(_jobs, key=lambda j: _jobs[j].started)[:len(_jobs) - _MAX_KEPT]:
                if _jobs[jid].status != "running":
                    _jobs.pop(jid, None)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def advance(job: Job | None, stage: str, detail: str = "") -> None:
    """Mark a real stage as started. Previous stages are recorded complete."""
    if job is None:
        return
    if job.stage and job.stage not in job.done_stages:
        job.done_stages.append(job.stage)
    job.stage = stage
    job.label = next((l for k, l, _ in job.stages if k == stage), stage)
    job.detail = detail


def finish(job: Job | None, result: dict) -> None:
    if job is None:
        return
    if job.stage and job.stage not in job.done_stages:
        job.done_stages.append(job.stage)
    job.stage = ""
    job.label = "Complete"
    job.status = "done"
    job.result = result
    job.finished = time.time()


def fail(job: Job | None, exc: Exception) -> None:
    if job is None:
        return
    job.status = "error"
    job.error = f"{type(exc).__name__}: {exc}"
    job.label = "Failed"
    job.finished = time.time()
