"""Download jobs.

A selection of stations can span several providers with wildly different
manners: Canada answers from a local SQLite file in milliseconds, Brazil sleeps
between requests, Japan scrapes a page per month. So work is grouped by
provider and each provider gets its own small worker pool — a slow provider
throttles only its own queue, never the whole job.

Jobs are fire-and-forget: ``POST /api/downloads`` returns immediately with a
job id, and progress arrives over server-sent events.
"""

from __future__ import annotations

import csv
import datetime
import logging
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from . import availability, paths, registry
from .catalog import catalog
from .registry import COUNTRY, GAUGE_ID, STATION_KEY, STATION_NAME

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
EMPTY = "empty"
UNSUPPORTED = "unsupported"
FAILED = "failed"
CANCELLED = "cancelled"
BLOCKED = "blocked"

TERMINAL_STATES = {DONE, EMPTY, UNSUPPORTED, FAILED, CANCELLED, BLOCKED}

#: Stations fetched at once from a single provider. One keeps every provider's
#: own politeness delays intact; providers reading from a warm local cache can
#: safely go wider.
DEFAULT_CONCURRENCY = 1
LOCAL_CACHE_CONCURRENCY = 4


def safe_filename(text: str) -> str:
    """Makes a gauge ID safe to use as a filename (Portugal's look like ``04K/04A``)."""
    return "".join(character if character.isalnum() or character in "-_." else "_" for character in text)


@dataclass
class FetchOutcome:
    """What one station's download produced: ``DONE``, ``EMPTY``, ``FAILED`` or ``BLOCKED``.

    A ``DONE`` outcome's message notes where the data came from when that is not
    the station's own provider (GRDC stations come from national services).
    """

    state: str
    frame: Optional[pd.DataFrame] = None
    rows: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    message: Optional[str] = None


def fetch_station(
    fetcher: Any,
    station_key: str,
    gauge_id: str,
    variable: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> FetchOutcome:
    """Downloads one station's series and records what it taught us about availability.

    Shared by server-side jobs and the hosted atlas's per-station CSV endpoint,
    so both classify a result the same way.
    """
    note = registry.station_download_note(fetcher, gauge_id)
    if note:
        return FetchOutcome(BLOCKED, message=note)

    try:
        frame = fetcher.get_data(gauge_id=gauge_id, variable=variable, start_date=start_date, end_date=end_date)
    except Exception as exc:
        message = str(exc)[:400]
        availability.record(station_key, variable, availability.FAILED, note=message)
        return FetchOutcome(FAILED, message=message)

    if frame is None or frame.empty:
        availability.record(station_key, variable, availability.ABSENT)
        # A fetcher may say what the station does have (GRDC: its GRDC-Caravan years).
        note = frame.attrs.get("coverage_note") if frame is not None else None
        return FetchOutcome(EMPTY, message=note or "The provider returned no rows for this range.")

    outcome = FetchOutcome(DONE, frame=frame, rows=len(frame), message=registry.source_note(frame))
    index = pd.to_datetime(frame.index, errors="coerce")
    if index.notna().any():
        outcome.first_date = str(index.min().date())
        outcome.last_date = str(index.max().date())
    availability.record(
        station_key,
        variable,
        availability.CONFIRMED,
        rows=outcome.rows,
        first_date=outcome.first_date,
        last_date=outcome.last_date,
    )
    return outcome


def _optional_text(value: Any) -> Optional[str]:
    """Catalog cells are NaN, not None, for the 48,397 stations that have no name."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)


@dataclass
class StationTask:
    """One station in a job."""

    station_key: str
    country: str
    gauge_id: str
    station_name: Optional[str] = None
    state: str = QUEUED
    rows: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    message: Optional[str] = None
    path: Optional[str] = None
    seconds: float = 0.0


@dataclass
class Job:
    """A download of one variable across many stations."""

    id: str
    variable: str
    start_date: Optional[str]
    end_date: Optional[str]
    created_at: str
    tasks: list[StationTask]
    state: str = QUEUED
    finished_at: Optional[str] = None
    unknown_keys: list[str] = field(default_factory=list)
    """Requested keys that are not in the catalog. Reported, never silently dropped."""

    events: list[dict[str, Any]] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- derived ---------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return paths.JOBS_DIR / self.id

    @property
    def csv_dir(self) -> Path:
        return self.directory / "data"

    @property
    def archive_path(self) -> Path:
        return self.directory / f"rivretrieve_{self.variable}_{self.id}.zip"

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.tasks:
            counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def progress(self) -> dict[str, Any]:
        counts = self.counts()
        finished = sum(counts.get(state, 0) for state in TERMINAL_STATES)
        return {
            "total": len(self.tasks),
            "finished": finished,
            "counts": counts,
            "state": self.state,
        }

    def to_dict(self, include_tasks: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "variable": self.variable,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "state": self.state,
            "progress": self.progress(),
            "has_archive": self.archive_path.exists(),
            "unknown_keys": self.unknown_keys,
        }
        if include_tasks:
            payload["tasks"] = [asdict(task) for task in self.tasks]
        return payload

    # -- events ----------------------------------------------------------------

    def emit(self, kind: str, **payload: Any) -> None:
        """Appends an event. SSE subscribers poll this list by index."""
        with self._lock:
            self.events.append(
                {
                    "seq": len(self.events),
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "kind": kind,
                    **payload,
                }
            )

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return self.events[seq:]

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


class JobManager:
    """Owns every job in the process and the fetcher instances they share."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._fetchers: dict[str, Any] = {}
        self._fetcher_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wateratlas-job")

    # -- fetchers --------------------------------------------------------------

    def fetcher(self, country: str):
        """One fetcher instance per provider, reused across jobs.

        Canada's fetcher resolves the HYDAT path on construction and Poland's
        opens a Zarr store, so rebuilding them per station would be wasteful.
        """
        with self._fetcher_lock:
            if country not in self._fetchers:
                self._fetchers[country] = registry.get_provider(country).build_fetcher()
            return self._fetchers[country]

    # -- job lifecycle ---------------------------------------------------------

    def create(
        self,
        station_keys: Iterable[str],
        variable: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Job:
        requested = list(station_keys)
        rows = catalog.rows_for_keys(requested)
        if rows.empty:
            raise ValueError("None of the requested stations are in the catalog.")

        found = set(rows[STATION_KEY])
        unknown = [key for key in requested if key not in found]

        tasks = [
            StationTask(
                station_key=row[STATION_KEY],
                country=row[COUNTRY],
                gauge_id=row[GAUGE_ID],
                station_name=_optional_text(row[STATION_NAME]),
            )
            for row in rows.to_dict(orient="records")
        ]

        job = Job(
            id=uuid.uuid4().hex[:12],
            variable=variable,
            start_date=start_date,
            end_date=end_date,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            tasks=tasks,
        )
        job.csv_dir.mkdir(parents=True, exist_ok=True)

        with self._jobs_lock:
            self._jobs[job.id] = job

        job.emit("job.created", total=len(tasks), variable=variable)
        if unknown:
            job.unknown_keys = unknown
            job.emit(
                "job.warning",
                message=f"{len(unknown)} requested station(s) are not in the catalog and were skipped.",
                station_keys=unknown[:20],
            )
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._jobs_lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    # -- execution -------------------------------------------------------------

    def _run(self, job: Job) -> None:
        job.state = RUNNING
        job.emit("job.started")

        by_country: dict[str, list[StationTask]] = {}
        for task in job.tasks:
            by_country.setdefault(task.country, []).append(task)

        # One worker per provider, so a slow provider never blocks a fast one.
        with ThreadPoolExecutor(max_workers=max(1, len(by_country))) as pool:
            futures = [
                pool.submit(self._run_provider, job, country, tasks)
                for country, tasks in by_country.items()
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Provider worker crashed: %s", exc)

        job.state = CANCELLED if job.cancelled else DONE
        job.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        try:
            self._write_manifest(job)
            self._build_archive(job)
        except Exception as exc:
            logger.exception("Could not package job %s: %s", job.id, exc)
            job.emit("job.warning", message=f"Could not build the archive: {exc}")
        job.emit("job.finished", state=job.state, progress=job.progress())

    def _run_provider(self, job: Job, country: str, tasks: list[StationTask]) -> None:
        provider = registry.get_provider(country)

        reason = provider.blocked_reason()
        if reason:
            for task in tasks:
                task.state = BLOCKED
                task.message = reason
                job.emit("station.finished", **asdict(task), progress=job.progress())
            return

        if job.variable not in provider.declared_variables():
            for task in tasks:
                task.state = UNSUPPORTED
                task.message = f"{provider.label} does not publish {job.variable}."
                job.emit("station.finished", **asdict(task), progress=job.progress())
            return

        try:
            fetcher = self.fetcher(country)
        except Exception as exc:
            for task in tasks:
                task.state = FAILED
                task.message = f"Could not start the {provider.label} fetcher: {exc}"
                job.emit("station.finished", **asdict(task), progress=job.progress())
            return

        if provider.bulk_note and not provider.cache_is_warm():
            job.emit(
                "job.notice",
                message=(
                    f"{provider.label} is downloading its bulk archive for the first time. "
                    "This can take a while; later jobs will reuse it."
                ),
            )

        workers = LOCAL_CACHE_CONCURRENCY if provider.cache_glob else DEFAULT_CONCURRENCY
        if workers <= 1:
            for task in tasks:
                if job.cancelled:
                    break
                self._run_station(job, provider, fetcher, task)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda task: self._run_station(job, provider, fetcher, task), tasks))

        if job.cancelled:
            for task in tasks:
                if task.state == QUEUED:
                    task.state = CANCELLED

    def _run_station(self, job: Job, provider: registry.Provider, fetcher: Any, task: StationTask) -> None:
        if job.cancelled:
            task.state = CANCELLED
            return

        task.state = RUNNING
        job.emit("station.started", station_key=task.station_key, country=task.country)
        started = time.monotonic()

        outcome = fetch_station(
            fetcher, task.station_key, task.gauge_id, job.variable, job.start_date, job.end_date
        )
        task.state = outcome.state
        task.message = outcome.message
        if outcome.state == DONE:
            path = self._write_station_csv(job, task, outcome.frame)
            task.rows = outcome.rows
            task.path = str(path.relative_to(job.csv_dir)).replace("\\", "/")
            task.first_date = outcome.first_date
            task.last_date = outcome.last_date

        task.seconds = round(time.monotonic() - started, 2)
        job.emit("station.finished", **asdict(task), progress=job.progress())

    def _write_station_csv(self, job: Job, task: StationTask, frame: pd.DataFrame) -> Path:
        directory = job.csv_dir / task.country
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_filename(task.gauge_id)}_{job.variable}.csv"
        frame.to_csv(path)
        return path

    # -- packaging -------------------------------------------------------------

    def _write_manifest(self, job: Job) -> None:
        """One row per requested station, including the ones that returned nothing."""
        path = job.directory / "manifest.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "station_key",
                    "country",
                    "provider",
                    "gauge_id",
                    "station_name",
                    "variable",
                    "status",
                    "rows",
                    "first_date",
                    "last_date",
                    "file",
                    "message",
                ]
            )
            for task in job.tasks:
                provider = registry.PROVIDERS_BY_KEY.get(task.country)
                writer.writerow(
                    [
                        task.station_key,
                        task.country,
                        provider.label if provider else "",
                        task.gauge_id,
                        task.station_name or "",
                        job.variable,
                        task.state,
                        task.rows,
                        task.first_date or "",
                        task.last_date or "",
                        task.path or "",
                        task.message or "",
                    ]
                )

    def _write_attribution(self, job: Job) -> str:
        """Provider credits for the archive.

        RivRetrieve's README is explicit that all data rights remain with the
        original providers and that users must check each provider's licence.
        An app that makes bulk download one click easy ships that with the data.
        """
        used = sorted({task.country for task in job.tasks if task.state == DONE})
        lines = [
            "# Data sources and attribution",
            "",
            f"Retrieved with RivRetrieve on {job.created_at} for variable `{job.variable}`.",
            "",
            "All data rights remain with the original providers. The MIT licence of the",
            "RivRetrieve code does **not** apply to the data in this archive. Before",
            "redistributing or publishing anything here, check each provider's own terms:",
            "",
        ]
        for country in used:
            provider = registry.PROVIDERS_BY_KEY.get(country)
            if provider is None:
                continue
            count = sum(1 for task in job.tasks if task.country == country and task.state == DONE)
            lines.append(
                f"- **{provider.label}** ({provider.country_name}) — "
                f"{count} station(s) — {provider.source_url}"
            )
        if "grdc" in used:
            lines += [
                "",
                "GRDC stations come from GRDC-Caravan, GRDC's open dataset (Global Runoff Data",
                "Centre, 2025, https://doi.org/10.5281/zenodo.15349031, CC BY 4.0: cite it when you",
                "use these series), or else from the national service that runs the station, whose",
                "terms apply. The `message` column of `manifest.csv` says which for each station.",
            ]
        lines += [
            "",
            "Units are SI throughout: discharge in m³/s, stage in m, water temperature in °C,",
            "precipitation in mm. Conversions were applied by RivRetrieve's fetchers.",
            "",
            "See `manifest.csv` for the status of every station that was requested,",
            "including those that returned no data.",
            "",
        ]
        return "\n".join(lines)

    def _build_archive(self, job: Job) -> None:
        """Bundles the CSVs, the manifest and the attribution notes into one ZIP."""
        if not any(task.state == DONE for task in job.tasks):
            return
        with zipfile.ZipFile(job.archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for task in job.tasks:
                if task.state != DONE or not task.path:
                    continue
                source = job.csv_dir / task.path
                if source.exists():
                    archive.write(source, arcname=f"data/{task.path}")
            manifest = job.directory / "manifest.csv"
            if manifest.exists():
                archive.write(manifest, arcname="manifest.csv")
            archive.writestr("ATTRIBUTION.md", self._write_attribution(job))


def estimate(station_keys: Iterable[str], variable: str) -> dict[str, Any]:
    """Pre-flight estimate shown before a job starts.

    Providers run in parallel, so the wall-clock estimate is the slowest
    provider's queue, not the sum of all of them.
    """
    rows = catalog.rows_for_keys(station_keys)
    per_country: dict[str, int] = {}
    for country in rows[COUNTRY]:
        per_country[country] = per_country.get(country, 0) + 1

    breakdown = []
    slowest = 0.0
    unsupported = 0
    blocked = 0
    for country, count in sorted(per_country.items(), key=lambda item: -item[1]):
        provider = registry.PROVIDERS_BY_KEY.get(country)
        if provider is None:
            continue
        supported = variable in provider.declared_variables()
        missing = provider.missing_credentials()
        reason = provider.blocked_reason()
        # Providers that serve only some of their stations (GRDC) are counted station by station.
        station_blocked = 0
        if supported and not reason and hasattr(provider.fetcher_class(), "unavailable_reason"):
            gauges = rows.loc[rows[COUNTRY] == country, GAUGE_ID]
            station_blocked = sum(
                1 for gauge in gauges if registry.station_download_note(provider.fetcher_class(), gauge)
            )
        if not supported:
            unsupported += count
        elif reason:
            blocked += count
        else:
            blocked += station_blocked
            workers = LOCAL_CACHE_CONCURRENCY if provider.uses_bulk_cache() else DEFAULT_CONCURRENCY
            slowest = max(slowest, (count - station_blocked) * provider.station_seconds() / workers)
        breakdown.append(
            {
                "country": country,
                "provider": provider.label,
                "stations": count,
                "supported": supported,
                "missing_credentials": missing,
                "blocked_reason": reason,
                "blocked_kind": provider.blocked_kind(),
                "unavailable_stations": station_blocked,
                "bulk_first_use": provider.bulk_note if not provider.cache_is_warm() else None,
                "throttle_note": provider.throttle_note,
            }
        )

    downloadable = len(rows) - unsupported - blocked
    return {
        "stations": len(rows),
        "downloadable": downloadable,
        "unsupported": unsupported,
        "blocked": blocked,
        "estimated_seconds": int(slowest),
        "breakdown": breakdown,
    }


def variable_choices(station_keys: Iterable[str]) -> list[dict[str, Any]]:
    """Variables worth offering for a selection, with how many stations declare each."""
    rows = catalog.rows_for_keys(station_keys)
    counts: dict[str, int] = {}
    for variables in rows[registry.VARIABLES]:
        for variable in variables:
            counts[variable] = counts.get(variable, 0) + 1
    return [
        {"variable": variable, "stations": count}
        for variable, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


#: Process-wide job manager.
manager = JobManager()
