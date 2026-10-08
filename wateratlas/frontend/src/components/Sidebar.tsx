import { useMemo, useState } from "react";
import { AUTHOR, COPYRIGHT_YEAR } from "../lib/about";
import {
  PROVIDER_REGIONS,
  colorForCountry,
  formatCount,
  groupVariables,
  variableLabel,
} from "../lib/palette";
import type { Filters, Provider, Station } from "../lib/types";

interface Props {
  providers: Provider[];
  variables: string[];
  /** Hosted on Vercel: credentials are the site's, not the visitor's to set. */
  hosted: boolean;
  filters: Filters;
  onFiltersChange: (next: Filters) => void;
  /** The station catalog is still loading into the worker. */
  loading: boolean;
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
  /** Phones only: the sidebar folds down to its header so the map gets the screen. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
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

/** One short line under a provider saying why, or how, its downloads are limited. */
function providerNote(provider: Provider, hosted: boolean): { text: string; warn: boolean } | null {
  switch (provider.blocked_kind) {
    case "credentials":
      return hosted
        ? { text: "needs an API key, not set up on this site", warn: true }
        : { text: `needs ${provider.missing_credentials.join(", ")} in .env`, warn: true };
    case "hosted":
      return { text: "map only here; download with the local app", warn: true };
  }
  if (provider.bulk_first_use && provider.cache_warm === false) {
    return { text: "one-time bulk download on first use", warn: false };
  }
  return null;
}

export function Sidebar({
  providers,
  variables,
  hosted,
  filters,
  onFiltersChange,
  loading,
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
  collapsed,
  onToggleCollapsed,
}: Props) {
  const [tab, setTab] = useState<"filters" | "results">("filters");

  const variableGroups = useMemo(() => groupVariables(variables), [variables]);

  const regions = useMemo(() => {
    const byKey = new Map(providers.map((provider) => [provider.key, provider]));
    const placed = new Set<string>();
    const groups = PROVIDER_REGIONS.map((region) => {
      const members = region.keys.flatMap((key) => {
        const provider = byKey.get(key);
        if (!provider) return [];
        placed.add(key);
        return [provider];
      });
      return { label: region.label, providers: members };
    });
    const others = providers.filter((provider) => !placed.has(provider.key));
    if (others.length) groups.push({ label: "Other", providers: others });
    return groups.filter((group) => group.providers.length > 0);
  }, [providers]);

  const toggle = (list: string[], value: string) =>
    list.includes(value) ? list.filter((item) => item !== value) : [...list, value];

  /** Selects every value in `values`, or clears them all if they were all selected. */
  const toggleAll = (list: string[], values: string[]) =>
    values.every((value) => list.includes(value))
      ? list.filter((item) => !values.includes(item))
      : [...list, ...values.filter((value) => !list.includes(value))];

  const activeFilterCount =
    filters.countries.length +
    filters.variables.length +
    (filters.text ? 1 : 0) +
    (filters.minArea !== null || filters.maxArea !== null ? 1 : 0);

  const credentialBlocked = providers.filter((provider) => provider.blocked_kind === "credentials");

  return (
    <aside className="riv-panel riv-sidebar">
      <header className="riv-sidebar-head">
        <div className="riv-sidebar-title">
          <img className="riv-brand-mark" src="/favicon.svg" width="30" height="30" alt="" />
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
          <button
            type="button"
            className="riv-button riv-collapse-toggle"
            aria-expanded={!collapsed}
            onClick={onToggleCollapsed}
          >
            {collapsed ? "Filters" : "Map"}
            {collapsed && activeFilterCount > 0 && (
              <span className="riv-count-badge">{activeFilterCount}</span>
            )}
          </button>
        </div>
        <p className="riv-muted riv-small riv-sidebar-summary">
          {loading ? (
            <>Loading {total > 0 ? formatCount(total) : ""} stations…</>
          ) : (
            <>
              <strong>{formatCount(matched)}</strong> of {formatCount(total)} stations from{" "}
              {providers.length} providers
              {offMap > 0 && (
                <span title={`${offMap} stations have no coordinates and are not on the map`}>
                  {" "}
                  · {formatCount(offMap)} unmapped
                </span>
              )}
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
          Filters{activeFilterCount > 0 && ` · ${activeFilterCount}`}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "results"}
          className={tab === "results" ? "is-active" : ""}
          onClick={() => setTab("results")}
        >
          Stations{loading ? "" : ` (${formatCount(resultsTotal)})`}
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
            <p className="riv-muted riv-small">Stations declaring any of the chosen series.</p>
            <div className="riv-variable-groups">
              {variableGroups.map((group) => {
                const groupVariables = group.items.map((item) => item.variable);
                const allOn = groupVariables.every((variable) => filters.variables.includes(variable));
                return (
                  <div key={group.quantity} className="riv-variable-group">
                    <button
                      type="button"
                      className="riv-group-label"
                      aria-pressed={allOn}
                      title={allOn ? `Clear every ${group.label.toLowerCase()} series` : `Any ${group.label.toLowerCase()} series`}
                      onClick={() =>
                        onFiltersChange({ ...filters, variables: toggleAll(filters.variables, groupVariables) })
                      }
                    >
                      {group.label}
                    </button>
                    <div className="riv-chips">
                      {group.items.map((item) => (
                        <button
                          key={item.variable}
                          type="button"
                          className="riv-chip-toggle"
                          aria-pressed={filters.variables.includes(item.variable)}
                          title={variableLabel(item.variable)}
                          onClick={() =>
                            onFiltersChange({ ...filters, variables: toggle(filters.variables, item.variable) })
                          }
                        >
                          {item.label}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
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
            {regions.map((region) => {
              const keys = region.providers.map((provider) => provider.key);
              const allOn = keys.every((key) => filters.countries.includes(key));
              return (
                <div key={region.label} className="riv-region">
                  <button
                    type="button"
                    className="riv-region-label"
                    aria-pressed={allOn}
                    title={allOn ? `Clear the ${region.label} providers` : `Filter to every ${region.label} provider`}
                    onClick={() =>
                      onFiltersChange({ ...filters, countries: toggleAll(filters.countries, keys) })
                    }
                  >
                    {region.label}
                  </button>
                  <ul className="riv-provider-list">
                    {region.providers.map((provider) => {
                      const note = providerNote(provider, hosted);
                      return (
                        <li key={provider.key}>
                          <label className="riv-check riv-provider-row">
                            <input
                              type="checkbox"
                              checked={filters.countries.includes(provider.key)}
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
                          {note && (
                            <p
                              className={note.warn ? "riv-provider-note riv-warn" : "riv-provider-note"}
                              title={provider.blocked_reason ?? provider.bulk_first_use ?? undefined}
                            >
                              {note.text}
                            </p>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })}
          </section>

          {!hosted && credentialBlocked.length > 0 && (
            <p className="riv-alert riv-alert-warn riv-small">
              {credentialBlocked.length} provider
              {credentialBlocked.length > 1 ? "s are" : " is"} unavailable until their credentials
              are set in <code>rivretrieve/.env</code>. Their stations still appear on the map.
            </p>
          )}

          {activeFilterCount > 0 && (
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
              disabled={loading || resultsTotal === 0}
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
          {loading && <p className="riv-muted riv-small">Loading stations…</p>}
          {!loading && resultsTotal === 0 && (
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
