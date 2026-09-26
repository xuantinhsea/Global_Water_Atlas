import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { colorForCountry, formatNumber, variableLabel, variableUnit } from "../lib/palette";
import type { Preview, Station, StationDetail } from "../lib/types";
import { Hydrograph } from "./Hydrograph";

interface Props {
  station: Station;
  isSelected: boolean;
  onToggleSelect: () => void;
  onClose: () => void;
  defaultStart: string;
  defaultEnd: string;
  /** Hosted on Vercel: downloads run in the browser via the cart panel. */
  hosted: boolean;
}

/** Provider-specific columns worth showing; the rest stay behind a disclosure. */
const PRIMARY_EXTRA = new Set([
  "stationTypeName",
  "stationStatusName",
  "countyName",
  "status",
  "dateOpened",
  "PROVINCIA",
  "TERMINO_MUNICIPAL",
  "Municipio_Nome",
  "UF_Nome_Estacao",
  "Sub_Bacia_Nome",
]);

export function StationPanel({
  station,
  isSelected,
  onToggleSelect,
  onClose,
  defaultStart,
  defaultEnd,
  hosted,
}: Props) {
  const [detail, setDetail] = useState<StationDetail | null>(null);
  const [variable, setVariable] = useState<string>(station.variables[0] ?? "");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAllExtra, setShowAllExtra] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setPreview(null);
    setError(null);
    setVariable(station.variables[0] ?? "");
    api
      .station(station.key)
      .then((result) => !cancelled && setDetail(result))
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
  }, [station.key]);

  const loadPreview = async () => {
    if (!variable) return;
    setLoadingPreview(true);
    setPreview(null);
    try {
      setPreview(await api.preview(station.key, variable, defaultStart, defaultEnd));
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setLoadingPreview(false);
    }
  };

  const downloadOne = () => {
    if (!variable) return;
    if (hosted) {
      // No server job to create; the cart panel fetches the CSV and builds the ZIP.
      window.dispatchEvent(
        new CustomEvent("atlas:download-request", { detail: { keys: [station.key], variable } }),
      );
      return;
    }
    api
      .createDownload([station.key], variable, defaultStart, defaultEnd)
      .then((job) => {
        // The job runs server-side; the cart panel picks it up and polls.
        window.dispatchEvent(new CustomEvent("atlas:job-created", { detail: job }));
      })
      .catch((exc: Error) => setError(exc.message));
  };

  const provider = detail?.provider;
  const blocked = provider?.missing_credentials?.length ? provider.missing_credentials : null;
  // Blocked for a reason other than credentials: a bulk-cache provider on the hosted atlas.
  const hostedOnlyBlock = !blocked && provider?.blocked_reason ? provider.blocked_reason : null;
  const unavailable = Boolean(blocked || hostedOnlyBlock);
  const extras = Object.entries(detail?.extra ?? {}).filter(
    ([key]) => !["gauge_id", "latitude", "longitude", "station_name", "river"].includes(key),
  );
  const shownExtras = showAllExtra ? extras : extras.filter(([key]) => PRIMARY_EXTRA.has(key));

  return (
    <aside className="riv-panel riv-station-panel">
      <header className="riv-panel-head">
        <div>
          <span
            className="riv-country-dot"
            style={{ background: colorForCountry(station.country) }}
            aria-hidden="true"
          />
          <span className="riv-eyebrow">{provider?.label ?? station.country}</span>
          <h2>{station.name ?? station.gaugeId}</h2>
          <p className="riv-mono riv-muted">{station.key}</p>
        </div>
        <button type="button" className="riv-icon-button" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      {error && <p className="riv-alert riv-alert-error">{error}</p>}

      <dl className="riv-facts">
        {detail?.river && (
          <div>
            <dt>River</dt>
            <dd>{detail.river}</dd>
          </div>
        )}
        <div>
          <dt>Coordinates</dt>
          <dd className="riv-mono">
            {station.lat.toFixed(5)}, {station.lon.toFixed(5)}
          </dd>
        </div>
        {detail?.area != null && (
          <div>
            <dt>Catchment</dt>
            <dd>{formatNumber(detail.area, 0)} km²</dd>
          </div>
        )}
        {detail?.altitude != null && (
          <div>
            <dt>Altitude</dt>
            <dd>{formatNumber(detail.altitude, 0)} m</dd>
          </div>
        )}
      </dl>

      <section className="riv-panel-section">
        <h3>Variables</h3>
        <p className="riv-muted riv-small">
          {provider?.key === "norway"
            ? "NVE publishes availability per station, so this list is exact."
            : "Declared by the provider — not every station carries every variable."}
        </p>
        <div className="riv-variable-list">
          {station.variables.map((name) => {
            const observed = detail?.observed?.[name];
            return (
              <label key={name} className="riv-variable-row">
                <input
                  type="radio"
                  name={`variable-${station.key}`}
                  value={name}
                  checked={variable === name}
                  onChange={() => {
                    setVariable(name);
                    setPreview(null);
                  }}
                />
                <span className="riv-variable-name">{variableLabel(name)}</span>
                <span className="riv-variable-unit riv-mono">{variableUnit(name)}</span>
                {observed?.state === "confirmed" && (
                  <span className="riv-chip riv-chip-ok" title={`${observed.rows} values downloaded`}>
                    confirmed {observed.first_date?.slice(0, 4)}–{observed.last_date?.slice(0, 4)}
                  </span>
                )}
                {observed?.state === "absent" && (
                  <span className="riv-chip riv-chip-muted">no data found</span>
                )}
              </label>
            );
          })}
          {!station.variables.length && (
            <p className="riv-muted riv-small">This station publishes no series.</p>
          )}
        </div>
      </section>

      {blocked && (
        <p className="riv-alert riv-alert-warn">
          {provider?.label} needs {blocked.join(" and ")} in <code>rivretrieve/.env</code> before
          anything can be downloaded.
        </p>
      )}
      {hostedOnlyBlock && <p className="riv-alert riv-alert-warn">{hostedOnlyBlock}</p>}
      {provider?.bulk_first_use && provider.cache_warm === false && !hostedOnlyBlock && (
        <p className="riv-alert riv-alert-warn">{provider.bulk_first_use}</p>
      )}
      {provider?.throttle_note && !unavailable && (
        <p className="riv-muted riv-small">{provider.throttle_note}</p>
      )}

      <div className="riv-panel-actions">
        <button
          type="button"
          className="riv-button"
          onClick={loadPreview}
          disabled={!variable || loadingPreview || unavailable}
        >
          {loadingPreview ? "Fetching…" : "Preview series"}
        </button>
        <button
          type="button"
          className="riv-button riv-button-primary"
          onClick={downloadOne}
          disabled={!variable || unavailable}
        >
          Download CSV
        </button>
        <button type="button" className="riv-button riv-button-ghost" onClick={onToggleSelect}>
          {isSelected ? "Remove from selection" : "Add to selection"}
        </button>
      </div>

      {loadingPreview && (
        <p className="riv-muted riv-small">
          Asking {provider?.label ?? "the provider"} for {defaultStart} to {defaultEnd}…
        </p>
      )}
      {preview && <Hydrograph preview={preview} />}

      {extras.length > 0 && (
        <section className="riv-panel-section">
          <h3>Provider record</h3>
          <dl className="riv-extra">
            {shownExtras.map(([key, value]) => (
              <div key={key}>
                <dt className="riv-mono">{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          {extras.length > shownExtras.length && (
            <button
              type="button"
              className="riv-link-button"
              onClick={() => setShowAllExtra(true)}
            >
              Show all {extras.length} fields
            </button>
          )}
        </section>
      )}

      {provider && (
        <footer className="riv-panel-foot">
          Data from{" "}
          <a href={provider.source_url} target="_blank" rel="noreferrer noopener">
            {provider.label}
          </a>
          . Rights remain with the provider.
        </footer>
      )}
    </aside>
  );
}
