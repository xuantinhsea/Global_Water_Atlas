import { useCallback, useEffect, useMemo, useState } from "react";
import { api, subscribeToJob } from "../lib/api";
import { formatCount, formatDuration, variableLabel } from "../lib/palette";
import type { Estimate, Job, JobTask } from "../lib/types";

interface Props {
  selection: Set<string>;
  onClear: () => void;
  onRemove: (key: string) => void;
  startDate: string;
  endDate: string;
  onDatesChange: (start: string, end: string) => void;
}

const TERMINAL = new Set(["done", "cancelled"]);

export function CartPanel({
  selection,
  onClear,
  onRemove,
  startDate,
  endDate,
  onDatesChange,
}: Props) {
  const keys = useMemo(() => Array.from(selection), [selection]);
  const [variableOptions, setVariableOptions] = useState<
    { variable: string; stations: number }[]
  >([]);
  const [variable, setVariable] = useState<string>("");
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);

  // Which variables make sense for this selection.
  useEffect(() => {
    if (!keys.length) {
      setVariableOptions([]);
      setEstimate(null);
      return;
    }
    let cancelled = false;
    api
      .variablesForSelection(keys)
      .then((result) => {
        if (cancelled) return;
        setVariableOptions(result.variables);
        setVariable((current) =>
          result.variables.some((option) => option.variable === current)
            ? current
            : (result.variables[0]?.variable ?? ""),
        );
      })
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
  }, [keys]);

  // Pre-flight estimate, refreshed whenever the selection or variable changes.
  useEffect(() => {
    if (!keys.length || !variable) {
      setEstimate(null);
      return;
    }
    let cancelled = false;
    api
      .estimate(keys, variable)
      .then((result) => !cancelled && setEstimate(result))
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
  }, [keys, variable]);

  const watchJob = useCallback((created: Job) => {
    setJob(created);
    const unsubscribe = subscribeToJob(created.id, (event) => {
      if (event.kind === "station.finished" || event.kind === "job.finished") {
        const progress = event.progress as Job["progress"] | undefined;
        if (progress) {
          setJob((current) => (current ? { ...current, progress, state: progress.state } : current));
        }
      }
      if (event.kind === "job.finished" || event.kind === "stream.closed") {
        api
          .job(created.id)
          .then(setJob)
          .catch(() => {
            /* the job list survives a failed refresh */
          });
      }
    });
    return unsubscribe;
  }, []);

  // A single-station download started from the detail panel lands here too.
  useEffect(() => {
    const onCreated = (event: Event) => {
      const created = (event as CustomEvent<Job>).detail;
      setOpen(true);
      watchJob(created);
    };
    window.addEventListener("atlas:job-created", onCreated);
    return () => window.removeEventListener("atlas:job-created", onCreated);
  }, [watchJob]);

  const start = async () => {
    if (!variable || !keys.length) return;
    setError(null);
    try {
      const created = await api.createDownload(keys, variable, startDate || null, endDate || null);
      watchJob(created);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      setJob(await api.cancelJob(job.id));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const running = job !== null && !TERMINAL.has(job.state);
  const counts = job?.progress.counts ?? {};
  const failures = (job?.tasks ?? []).filter((task) =>
    ["failed", "blocked", "unsupported"].includes(task.state),
  );

  if (!keys.length && !job) return null;

  return (
    <aside className={`riv-panel riv-cart ${open ? "" : "is-collapsed"}`}>
      <header className="riv-cart-head">
        <button
          type="button"
          className="riv-cart-toggle"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
        >
          <span className="riv-eyebrow">Selection</span>
          <strong>{formatCount(keys.length)} stations</strong>
          <span aria-hidden="true">{open ? "▾" : "▴"}</span>
        </button>
        {keys.length > 0 && (
          <button type="button" className="riv-link-button" onClick={onClear}>
            Clear
          </button>
        )}
      </header>

      {open && (
        <div className="riv-cart-body">
          {error && <p className="riv-alert riv-alert-error">{error}</p>}

          {keys.length > 0 && (
            <>
              <div className="riv-field">
                <label htmlFor="riv-variable">Variable</label>
                <select
                  id="riv-variable"
                  value={variable}
                  onChange={(event) => setVariable(event.target.value)}
                >
                  {variableOptions.map((option) => (
                    <option key={option.variable} value={option.variable}>
                      {variableLabel(option.variable)} — {formatCount(option.stations)} stations
                    </option>
                  ))}
                </select>
              </div>

              <div className="riv-field riv-dates">
                <div>
                  <label htmlFor="riv-start">From</label>
                  <input
                    id="riv-start"
                    type="date"
                    value={startDate}
                    onChange={(event) => onDatesChange(event.target.value, endDate)}
                  />
                </div>
                <div>
                  <label htmlFor="riv-end">To</label>
                  <input
                    id="riv-end"
                    type="date"
                    value={endDate}
                    onChange={(event) => onDatesChange(startDate, event.target.value)}
                  />
                </div>
              </div>

              {estimate && (
                <div className="riv-estimate">
                  <p>
                    <strong>{formatCount(estimate.downloadable)}</strong> will download
                    {estimate.unsupported > 0 && (
                      <> · {formatCount(estimate.unsupported)} do not publish this variable</>
                    )}
                    {estimate.blocked > 0 && (
                      <> · {formatCount(estimate.blocked)} blocked by missing credentials</>
                    )}
                  </p>
                  <p className="riv-muted riv-small">
                    Roughly {formatDuration(estimate.estimated_seconds)} across{" "}
                    {estimate.breakdown.length} provider
                    {estimate.breakdown.length > 1 ? "s" : ""}.
                  </p>
                  <ul className="riv-estimate-list">
                    {estimate.breakdown.map((row) => (
                      <li key={row.country}>
                        <span className="riv-estimate-provider">{row.provider}</span>
                        <span className="riv-mono riv-tabular">{formatCount(row.stations)}</span>
                        {!row.supported && <em className="riv-warn">no {variable}</em>}
                        {row.missing_credentials.length > 0 && (
                          <em className="riv-warn">needs {row.missing_credentials.join(", ")}</em>
                        )}
                        {row.bulk_first_use && <em className="riv-warn">bulk download first</em>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <button
                type="button"
                className="riv-button riv-button-primary riv-full"
                onClick={start}
                disabled={running || !variable || (estimate?.downloadable ?? 0) === 0}
              >
                {running ? "Downloading…" : `Download ${formatCount(estimate?.downloadable ?? keys.length)} stations`}
              </button>
            </>
          )}

          {job && (
            <section className="riv-job">
              <div className="riv-job-head">
                <span className="riv-eyebrow">{variableLabel(job.variable)}</span>
                <span className="riv-mono riv-muted">{job.id}</span>
              </div>

              <div
                className="riv-progress"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={job.progress.total}
                aria-valuenow={job.progress.finished}
              >
                <span
                  style={{
                    width: `${(job.progress.finished / Math.max(1, job.progress.total)) * 100}%`,
                  }}
                />
              </div>

              <p className="riv-small">
                {job.progress.finished} of {job.progress.total} ·{" "}
                {Object.entries(counts)
                  .map(([state, count]) => `${count} ${state}`)
                  .join(" · ")}
              </p>

              {job.unknown_keys.length > 0 && (
                <p className="riv-muted riv-small">
                  {job.unknown_keys.length} requested station
                  {job.unknown_keys.length > 1 ? "s were" : " was"} not in the catalog and{" "}
                  {job.unknown_keys.length > 1 ? "were" : "was"} skipped.
                </p>
              )}

              <div className="riv-job-actions">
                {running ? (
                  <button type="button" className="riv-button riv-button-ghost" onClick={cancel}>
                    Cancel
                  </button>
                ) : (
                  job.has_archive && (
                    <a className="riv-button riv-button-primary" href={api.archiveUrl(job.id)}>
                      Download ZIP
                    </a>
                  )
                )}
              </div>

              {TERMINAL.has(job.state) && !job.has_archive && (
                <p className="riv-alert riv-alert-warn riv-small">
                  No station returned data, so there is no archive. See the failures below.
                </p>
              )}

              {failures.length > 0 && (
                <details className="riv-failures">
                  <summary>{failures.length} did not return data</summary>
                  <ul>
                    {failures.slice(0, 40).map((task: JobTask) => (
                      <li key={task.station_key}>
                        <span className="riv-mono">{task.station_key}</span>
                        <span className={`riv-chip riv-chip-${task.state}`}>{task.state}</span>
                        {task.message && <em>{task.message}</em>}
                      </li>
                    ))}
                  </ul>
                  {failures.length > 40 && (
                    <p className="riv-muted riv-small">
                      The full list is in <code>manifest.csv</code>.
                    </p>
                  )}
                </details>
              )}
            </section>
          )}

          {keys.length > 0 && keys.length <= 40 && (
            <details className="riv-selection-list">
              <summary>Selected stations</summary>
              <ul>
                {keys.map((key) => (
                  <li key={key}>
                    <span className="riv-mono">{key}</span>
                    <button
                      type="button"
                      className="riv-link-button"
                      onClick={() => onRemove(key)}
                      aria-label={`Remove ${key}`}
                    >
                      remove
                    </button>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </aside>
  );
}
