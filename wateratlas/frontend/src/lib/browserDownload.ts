/**
 * Download jobs that run in the browser, for the hosted atlas.
 *
 * Hosted on Vercel, no server process lives long enough to run a batch job, so
 * the browser does what `wateratlas/jobs.py` does locally: it fetches each
 * station's CSV from `/api/station/data`, one at a time per provider (several
 * providers at once), and packs the results into the same archive layout —
 * `data/<provider>/<gauge>_<variable>.csv`, `manifest.csv` and `ATTRIBUTION.md`.
 */

import { api, type SelectedStation } from "./api";
import type { Job, JobTask, Provider, TaskState } from "./types";
import { buildZip, type Bytes, type ZipEntry } from "./zip";

const TERMINAL: TaskState[] = ["done", "empty", "unsupported", "failed", "cancelled", "blocked"];

/** UTC timestamp in the same shape the server writes (`2026-09-26T12:00:00+00:00`). */
const isoNow = () => new Date().toISOString().slice(0, 19) + "+00:00";

export interface BrowserJobOptions {
  keys: string[];
  variable: string;
  startDate: string | null;
  endDate: string | null;
  providers: Provider[];
  /** Called after every change; `archive` is set once the job has finished with data. */
  onUpdate: (job: Job, archive: Blob | null) => void;
}

export interface BrowserJobHandle {
  cancel: () => void;
}

/** Same rule as `jobs.safe_filename`: Portuguese gauge IDs look like `04K/04A`. */
export function safeFilename(text: string): string {
  return Array.from(text, (character) => (/[\p{L}\p{N}\-_.]/u.test(character) ? character : "_")).join("");
}

export function archiveName(job: Job): string {
  return `rivretrieve_${job.variable}_${job.id}.zip`;
}

function csvCell(value: string | number | null): string {
  const text = value === null ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function manifest(job: Job, tasks: JobTask[], providers: Map<string, Provider>): string {
  const header = [
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
  ];
  const rows = tasks.map((task) =>
    [
      task.station_key,
      task.country,
      providers.get(task.country)?.label ?? "",
      task.gauge_id,
      task.station_name,
      job.variable,
      task.state,
      task.rows,
      task.first_date,
      task.last_date,
      task.path,
      task.message,
    ]
      .map(csvCell)
      .join(","),
  );
  return [header.join(","), ...rows].join("\r\n") + "\r\n";
}

function attribution(job: Job, tasks: JobTask[], providers: Map<string, Provider>): string {
  const used = Array.from(new Set(tasks.filter((t) => t.state === "done").map((t) => t.country))).sort();
  const lines = [
    "# Data sources and attribution",
    "",
    `Retrieved with RivRetrieve (hosted Global Water Atlas) on ${job.created_at} for variable \`${job.variable}\`.`,
    "",
    "All data rights remain with the original providers. The MIT licence of the",
    "RivRetrieve code does **not** apply to the data in this archive. Before",
    "redistributing or publishing anything here, check each provider's own terms:",
    "",
  ];
  for (const country of used) {
    const provider = providers.get(country);
    if (!provider) continue;
    const count = tasks.filter((t) => t.country === country && t.state === "done").length;
    lines.push(
      `- **${provider.label}** (${provider.country_name}) — ${count} station(s) — ${provider.source_url}`,
    );
  }
  if (used.includes("grdc")) {
    lines.push(
      "",
      "GRDC stations were downloaded from the national service that runs each of them,",
      "whose terms apply to that data; the `message` column of `manifest.csv` names the",
      "service and its station ID.",
    );
  }
  lines.push(
    "",
    "Units are SI throughout: discharge in m³/s, stage in m, water temperature in °C,",
    "precipitation in mm. Conversions were applied by RivRetrieve's fetchers.",
    "",
    "See `manifest.csv` for the status of every station that was requested,",
    "including those that returned no data.",
    "",
  );
  return lines.join("\n");
}

export function runBrowserJob(options: BrowserJobOptions): BrowserJobHandle {
  const { keys, variable, startDate, endDate, onUpdate } = options;
  const providers = new Map(options.providers.map((provider) => [provider.key, provider]));
  const controller = new AbortController();
  const files = new Map<string, Bytes>();
  let tasks: JobTask[] = [];

  const job: Job = {
    id: crypto.randomUUID().replace(/-/g, "").slice(0, 12),
    variable,
    start_date: startDate,
    end_date: endDate,
    created_at: isoNow(),
    finished_at: null,
    state: "queued",
    progress: { total: keys.length, finished: 0, counts: {}, state: "queued" },
    has_archive: false,
    unknown_keys: [],
    tasks: [],
  };

  const publish = (archive: Blob | null = null) => {
    const counts: Partial<Record<TaskState, number>> = {};
    for (const task of tasks) counts[task.state] = (counts[task.state] ?? 0) + 1;
    job.progress = {
      total: tasks.length,
      finished: tasks.filter((task) => TERMINAL.includes(task.state)).length,
      counts,
      state: job.state,
    };
    onUpdate({ ...job, tasks: tasks.map((task) => ({ ...task })) }, archive);
  };

  const runTask = async (task: JobTask) => {
    if (controller.signal.aborted) {
      task.state = "cancelled";
      return;
    }
    task.state = "running";
    publish();
    const started = performance.now();
    try {
      const result = await api.stationData(task.station_key, variable, startDate, endDate, controller.signal);
      if (result.status === "done") {
        task.path = `${task.country}/${safeFilename(task.gauge_id)}_${variable}.csv`;
        files.set(task.path, result.csv);
        task.rows = result.rows;
        task.first_date = result.firstDate;
        task.last_date = result.lastDate;
        task.message = result.note;
        task.state = "done";
      } else {
        task.state = result.status;
        task.message = result.message || null;
      }
    } catch (exc) {
      task.state = controller.signal.aborted ? "cancelled" : "failed";
      task.message = controller.signal.aborted ? null : (exc as Error).message;
    }
    task.seconds = Math.round((performance.now() - started) / 10) / 100;
    publish();
  };

  const run = async () => {
    job.state = "running";
    publish();

    let selected: SelectedStation[];
    try {
      const response = await api.selectionStations(keys);
      selected = response.stations;
      job.unknown_keys = response.unknown_keys;
    } catch (exc) {
      // Without the catalog lookup nothing can be named or grouped; fail every key.
      tasks = keys.map((key) => ({
        station_key: key,
        country: key.split(":")[0],
        gauge_id: key.split(":").slice(1).join(":"),
        station_name: null,
        state: "failed",
        rows: 0,
        first_date: null,
        last_date: null,
        message: (exc as Error).message,
        path: null,
        seconds: 0,
      }));
      job.state = "done";
      job.finished_at = isoNow();
      publish();
      return;
    }

    tasks = selected.map((station) => ({
      ...station,
      state: "queued",
      rows: 0,
      first_date: null,
      last_date: null,
      message: null,
      path: null,
      seconds: 0,
    }));
    publish();

    // One queue per provider, run side by side: a slow provider only holds up its own
    // stations, and no provider sees more than one request from this job at a time.
    const byProvider = new Map<string, JobTask[]>();
    for (const task of tasks) byProvider.set(task.country, [...(byProvider.get(task.country) ?? []), task]);
    await Promise.all(
      Array.from(byProvider.values(), async (queue) => {
        for (const task of queue) await runTask(task);
      }),
    );

    job.state = controller.signal.aborted ? "cancelled" : "done";
    job.finished_at = isoNow();

    let archive: Blob | null = null;
    if (tasks.some((task) => task.state === "done")) {
      const entries: ZipEntry[] = [];
      for (const task of tasks) {
        const data = task.path ? files.get(task.path) : undefined;
        if (task.state === "done" && task.path && data) entries.push({ name: `data/${task.path}`, data });
      }
      entries.push({ name: "manifest.csv", data: manifest(job, tasks, providers) });
      entries.push({ name: "ATTRIBUTION.md", data: attribution(job, tasks, providers) });
      archive = buildZip(entries);
      job.has_archive = true;
      files.clear();
    }
    publish(archive);
  };

  void run();
  return { cancel: () => controller.abort() };
}
