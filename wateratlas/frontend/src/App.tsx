import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AboutDialog } from "./components/AboutDialog";
import { CartPanel } from "./components/CartPanel";
import { MapView } from "./components/MapView";
import { Sidebar } from "./components/Sidebar";
import { StationPanel } from "./components/StationPanel";
import { api } from "./lib/api";
import { ClusterClient } from "./lib/clusterClient";
import { EMPTY_FILTERS } from "./lib/types";
import type { Filters, ProvidersResponse, Station } from "./lib/types";

const PAGE_SIZE = 80;

/** A decade of daily data is a sensible opening range: useful, not punishing. */
function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end.getFullYear() - 10, end.getMonth(), end.getDate());
  const iso = (date: Date) => date.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

export default function App() {
  const clientRef = useRef<ClusterClient | null>(null);
  const [client, setClient] = useState<ClusterClient | null>(null);
  const [boot, setBoot] = useState<{ state: "loading" | "ready" | "error"; message?: string }>({
    state: "loading",
  });

  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [filtersVersion, setFiltersVersion] = useState(0);
  const [matched, setMatched] = useState(0);
  const [catalogInfo, setCatalogInfo] = useState<{ total: number; offMap: number } | null>(null);

  const [results, setResults] = useState<Station[]>([]);
  const [resultsTotal, setResultsTotal] = useState(0);
  const [page, setPage] = useState(1);

  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<Station | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // `#about` in the URL opens the About dialog directly, so it can be linked to.
  const [aboutOpen, setAboutOpen] = useState(() => window.location.hash === "#about");

  const initialRange = useMemo(defaultRange, []);
  const [startDate, setStartDate] = useState(initialRange.start);
  const [endDate, setEndDate] = useState(initialRange.end);

  // -- boot --------------------------------------------------------------------

  useEffect(() => {
    const worker = new ClusterClient();
    clientRef.current = worker;
    worker.onMatchedChange = setMatched;

    Promise.all([worker.load(EMPTY_FILTERS), api.providers()])
      .then(([info, providerList]) => {
        setCatalogInfo({ total: info.total, offMap: info.offMap });
        setProviders(providerList);
        setClient(worker);
        setBoot({ state: "ready" });
      })
      .catch((exc: Error) => setBoot({ state: "error", message: exc.message }));

    return () => {
      worker.dispose();
      clientRef.current = null;
    };
  }, []);

  // -- filters -----------------------------------------------------------------

  const applyFilters = useCallback((next: Filters) => {
    setFilters(next);
    setPage(1);
    clientRef.current?.setFilters(next);
    setFiltersVersion((version) => version + 1);
  }, []);

  // Results list follows the filters and the page size.
  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    client.list(0, page * PAGE_SIZE).then((result) => {
      if (cancelled) return;
      setResults(result.stations);
      setResultsTotal(result.total);
    });
    return () => {
      cancelled = true;
    };
  }, [client, page, filtersVersion, matched]);

  // -- selection ---------------------------------------------------------------

  const selectKeys = useCallback((keys: string[], mode: "add" | "replace") => {
    setSelection((current) => {
      const next = mode === "replace" ? new Set<string>() : new Set(current);
      for (const key of keys) next.add(key);
      return next;
    });
  }, []);

  const toggleStation = useCallback((key: string) => {
    setSelection((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const selectAllFiltered = useCallback(async () => {
    if (!client) return;
    const result = await client.allFilteredKeys(5000);
    selectKeys(result.keys, "replace");
    if (result.truncated) {
      setNotice("Selection capped at 5,000 stations — narrow the filters to reach the rest.");
    }
  }, [client, selectKeys]);

  const onSelectionTruncated = useCallback((limit: number) => {
    setNotice(`Selection capped at ${limit.toLocaleString()} stations.`);
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

  // -- render ------------------------------------------------------------------

  if (boot.state === "error") {
    return (
      <div className="riv-boot">
        <div>
          <h1>The atlas could not start</h1>
          <p className="riv-alert riv-alert-error">{boot.message}</p>
          <p className="riv-muted">
            The station catalog is built from the cached provider CSVs. From the repository root:
          </p>
          <pre>python scripts/build_catalog.py</pre>
          <p className="riv-muted riv-small">
            Then reload this page. <code>wateratlas doctor</code> reports anything else that is
            missing.
          </p>
        </div>
      </div>
    );
  }

  if (boot.state === "loading") {
    return (
      <div className="riv-boot">
        <div>
          <h1>Global Water Atlas</h1>
          <p className="riv-muted">Loading the station catalog…</p>
        </div>
      </div>
    );
  }

  return (
    <div className={active ? "riv-app has-panel" : "riv-app"}>
      <Sidebar
        providers={providers?.providers ?? []}
        variables={providers?.variables ?? []}
        filters={filters}
        onFiltersChange={applyFilters}
        matched={matched}
        total={catalogInfo?.total ?? 0}
        offMap={catalogInfo?.offMap ?? 0}
        results={results}
        resultsTotal={resultsTotal}
        selection={selection}
        onPickStation={setActive}
        onToggleStation={toggleStation}
        onSelectAllFiltered={selectAllFiltered}
        onLoadMore={() => setPage((current) => current + 1)}
        onOpenAbout={() => setAboutOpen(true)}
      />

      <AboutDialog
        open={aboutOpen}
        onClose={() => {
          setAboutOpen(false);
          if (window.location.hash === "#about") {
            history.replaceState(null, "", window.location.pathname + window.location.search);
          }
        }}
        // `total` counts mappable stations; the catalog also holds those without coordinates.
        stationCount={(catalogInfo?.total ?? 0) + (catalogInfo?.offMap ?? 0)}
        providerCount={providers?.providers.length ?? 0}
      />

      <main className="riv-main">
        <MapView
          client={client}
          filtersVersion={filtersVersion}
          selection={selection}
          onPickStation={setActive}
          onSelectKeys={selectKeys}
          onSelectionTruncated={onSelectionTruncated}
        />
        {notice && <div className="riv-toast">{notice}</div>}
      </main>

      {active && (
        <StationPanel
          station={active}
          isSelected={selection.has(active.key)}
          onToggleSelect={() => toggleStation(active.key)}
          onClose={() => setActive(null)}
          defaultStart={startDate}
          defaultEnd={endDate}
        />
      )}

      <CartPanel
        selection={selection}
        onClear={() => setSelection(new Set())}
        onRemove={toggleStation}
        startDate={startDate}
        endDate={endDate}
        onDatesChange={(start, end) => {
          setStartDate(start);
          setEndDate(end);
        }}
      />
    </div>
  );
}
