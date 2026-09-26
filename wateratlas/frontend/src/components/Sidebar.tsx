import { useMemo, useState } from "react";
import { AUTHOR, COPYRIGHT_YEAR } from "../lib/about";
import { colorForCountry, formatCount, variableLabel } from "../lib/palette";
import type { Filters, Provider, Station } from "../lib/types";

interface Props {
  providers: Provider[];
  variables: string[];
  filters: Filters;
  onFiltersChange: (next: Filters) => void;
  matched: number;
  total: number;
  offMap: number;
  results: Station[];
  resultsTotal: number;
  selection: Set<string>;
  onPickStation: (station: Station) => void;
  onToggleStation: (key: string) => void;
  onSelectAllFiltered: () => void;
  onLoadMore: () => void;
  onOpenAbout: () => void;
}

function InfoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <circle cx="8" cy="8" r="6.9" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="8" cy="4.9" r="0.95" fill="currentColor" />
      <path d="M8 7.2v4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function Sidebar({
  providers,
  variables,
  filters,
  onFiltersChange,
  matched,
  total,
  offMap,
  results,
  resultsTotal,
  selection,
  onPickStation,
  onToggleStation,
  onSelectAllFiltered,
  onLoadMore,
  onOpenAbout,
}: Props) {
  const [tab, setTab] = useState<"filters" | "results">("filters");

  const blockedProviders = useMemo(
    () => providers.filter((provider) => provider.missing_credentials.length > 0),
    [providers],
  );

  const toggle = (list: string[], value: string) =>
    list.includes(value) ? list.filter((item) => item !== value) : [...list, value];

  const isFiltered =
    filters.countries.length > 0 ||
    filters.variables.length > 0 ||
    filters.text.length > 0 ||
    filters.minArea !== null ||
    filters.maxArea !== null;

  return (
    <aside className="riv-panel riv-sidebar">
      <header className="riv-sidebar-head">
        <div className="riv-sidebar-title">
          <h1>Global Water Atlas</h1>
          <button
            type="button"
            className="riv-icon-button riv-info-button"
            aria-label="About Global Water Atlas, copyright and contact"
            title="About, copyright and contact"
            onClick={onOpenAbout}
          >
            <InfoIcon />
          </button>
        </div>
        <p className="riv-muted riv-small">
          <strong>{formatCount(matched)}</strong> of {formatCount(total)} stations
          {offMap > 0 && (
            <>
              {" "}
              · {offMap} have no coordinates and are not on the map
            </>
          )}
        </p>
      </header>

      <div className="riv-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "filters"}
          className={tab === "filters" ? "is-active" : ""}
          onClick={() => setTab("filters")}
        >
          Filters
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "results"}
          className={tab === "results" ? "is-active" : ""}
          onClick={() => setTab("results")}
        >
          Stations ({formatCount(resultsTotal)})
        </button>
      </div>

      {tab === "filters" ? (
        <div className="riv-sidebar-body">
          <div className="riv-field">
            <label htmlFor="riv-search">Search</label>
            <input
              id="riv-search"
              type="search"
              placeholder="Station name or gauge ID"
              value={filters.text}
              onChange={(event) => onFiltersChange({ ...filters, text: event.target.value })}
            />
          </div>

          <section className="riv-filter-section">
            <h3>Variable</h3>
            <p className="riv-muted riv-small">Stations declaring any of these.</p>
            <div className="riv-checks">
              {variables.map((variable) => (
                <label key={variable} className="riv-check">
                  <input
                    type="checkbox"
                    checked={filters.variables.includes(variable)}
                    onChange={() =>
                      onFiltersChange({
                        ...filters,
                        variables: toggle(filters.variables, variable),
                      })
                    }
                  />
                  <span>{variableLabel(variable)}</span>
                </label>
              ))}
            </div>
          </section>

          <section className="riv-filter-section">
            <h3>Catchment area</h3>
            <div className="riv-range">
              <input
                type="number"
                min={0}
                placeholder="min"
                aria-label="Minimum catchment area in km²"
                value={filters.minArea ?? ""}
                onChange={(event) =>
                  onFiltersChange({
                    ...filters,
                    minArea: event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              />
              <span className="riv-muted">–</span>
              <input
                type="number"
                min={0}
                placeholder="max"
                aria-label="Maximum catchment area in km²"
                value={filters.maxArea ?? ""}
                onChange={(event) =>
                  onFiltersChange({
                    ...filters,
                    maxArea: event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              />
              <span className="riv-muted riv-small">km²</span>
            </div>
            <p className="riv-muted riv-small">
              Only some providers publish catchment area; stations without it are excluded when
              this filter is set.
            </p>
          </section>

          <section className="riv-filter-section">
            <h3>Provider</h3>
            <ul className="riv-provider-list">
              {providers.map((provider) => {
                const active = filters.countries.includes(provider.key);
                return (
                  <li key={provider.key}>
                    <label className="riv-check riv-provider-row">
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() =>
                          onFiltersChange({
                            ...filters,
                            countries: toggle(filters.countries, provider.key),
                          })
                        }
                      />
                      <span
                        className="riv-country-dot"
                        style={{ background: colorForCountry(provider.key) }}
                        aria-hidden="true"
                      />
                      <span className="riv-provider-name">
                        {provider.country_name}
                        <em>{provider.label}</em>
                      </span>
                      <span className="riv-mono riv-muted riv-tabular">
                        {formatCount(provider.stations)}
                      </span>
                    </label>
                    {provider.missing_credentials.length > 0 && (
                      <p className="riv-provider-note riv-warn">
                        needs {provider.missing_credentials.join(", ")}
                      </p>
                    )}
                    {provider.bulk_first_use && provider.cache_warm === false && (
                      <p className="riv-provider-note">one-time bulk download on first use</p>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          {blockedProviders.length > 0 && (
            <p className="riv-alert riv-alert-warn riv-small">
              {blockedProviders.length} provider
              {blockedProviders.length > 1 ? "s are" : " is"} unavailable until their credentials
              are set in <code>rivretrieve/.env</code>. Their stations still appear on the map.
            </p>
          )}

          {isFiltered && (
            <button
              type="button"
              className="riv-button riv-button-ghost riv-full"
              onClick={() =>
                onFiltersChange({
                  countries: [],
                  variables: [],
                  text: "",
                  minArea: null,
                  maxArea: null,
                })
              }
            >
              Clear all filters
            </button>
          )}
        </div>
      ) : (
        <div className="riv-sidebar-body">
          <div className="riv-results-actions">
            <button
              type="button"
              className="riv-button riv-button-ghost"
              onClick={onSelectAllFiltered}
              disabled={resultsTotal === 0}
            >
              Select all {formatCount(Math.min(resultsTotal, 5000))}
            </button>
          </div>

          <ul className="riv-results">
            {results.map((station) => (
              <li key={station.key}>
                <label className="riv-result-check">
                  <input
                    type="checkbox"
                    checked={selection.has(station.key)}
                    onChange={() => onToggleStation(station.key)}
                    aria-label={`Select ${station.name ?? station.gaugeId}`}
                  />
                </label>
                <button
                  type="button"
                  className="riv-result-button"
                  onClick={() => onPickStation(station)}
                >
                  <span
                    className="riv-country-dot"
                    style={{ background: colorForCountry(station.country) }}
                    aria-hidden="true"
                  />
                  <span className="riv-result-text">
                    <strong>{station.name ?? station.gaugeId}</strong>
                    <span className="riv-mono riv-muted">{station.key}</span>
                  </span>
                  {station.area != null && (
                    <span className="riv-mono riv-muted riv-tabular">
                      {Math.round(station.area).toLocaleString()} km²
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>

          {results.length < resultsTotal && (
            <button type="button" className="riv-button riv-button-ghost riv-full" onClick={onLoadMore}>
              Show more ({formatCount(resultsTotal - results.length)} left)
            </button>
          )}
          {resultsTotal === 0 && (
            <p className="riv-muted riv-small">No stations match these filters.</p>
          )}
        </div>
      )}

      <footer className="riv-sidebar-foot">
        <span>
          © {COPYRIGHT_YEAR} {AUTHOR.shortName}
        </span>
        <button type="button" className="riv-link-button" onClick={onOpenAbout}>
          About &amp; contact
        </button>
      </footer>
    </aside>
  );
}
