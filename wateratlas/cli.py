"""Command line for the Global Water Atlas.

    wateratlas build-catalog     normalise every provider's cached CSV into one index
    wateratlas doctor            report what is ready and what is missing
    wateratlas warm              pre-fetch the Canada and Poland bulk caches
    wateratlas serve             run the app (default)
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from threading import Timer

from . import availability, paths, registry
from .catalog_build import build_catalog


def _cmd_build_catalog(args: argparse.Namespace) -> int:
    print("Building station catalog from rivretrieve/cached_site_data/ ...")
    summary = build_catalog(verbose=True)
    return 0 if summary["totals"]["providers_failed"] == 0 else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Prints everything that decides whether a station can be downloaded."""
    ok = True

    print("Catalog")
    if paths.STATIONS_PARQUET.exists():
        from .catalog import catalog

        totals = catalog.totals()
        print(f"  built    {totals['stations']:,} stations, {totals['mappable']:,} mappable")
        print(f"  unnamed  {totals['stations'] - totals['named']:,} stations have no name")
    else:
        ok = False
        print(f"  MISSING  no catalog at {paths.STATIONS_PARQUET}")
        print("           run: wateratlas build-catalog")

    print("\nFront end")
    if paths.FRONTEND_DIST.exists():
        print(f"  built    {paths.FRONTEND_DIST}")
    else:
        print(f"  MISSING  {paths.FRONTEND_DIST}")
        print(f"           run: cd {paths.FRONTEND_DIR} && npm install && npm run build")

    print("\nCredentials")
    blocked = [p for p in registry.PROVIDERS if p.missing_credentials()]
    if not blocked:
        print("  ok       every provider that needs credentials has them")
    for provider in blocked:
        print(
            f"  MISSING  {provider.label} ({provider.country_name}) needs "
            f"{', '.join(provider.missing_credentials())}"
        )
        print(f"           set them in {paths.RIVRETRIEVE_DIR / '.env'}")

    print("\nBulk caches")
    for provider in registry.PROVIDERS:
        if not provider.bulk_first_use:
            continue
        state = "warm" if provider.cache_is_warm() else "cold"
        print(f"  {state:8s} {provider.label} — {provider.bulk_first_use}")
        if state == "cold":
            print(f"           run: wateratlas warm --provider {provider.key}")

    print("\nLearned availability")
    availability.init()
    counts = availability.summary()
    if counts.get("total"):
        print(
            f"  {counts.get('confirmed', 0):,} confirmed, {counts.get('absent', 0):,} absent, "
            f"{counts.get('failed', 0):,} failed"
        )
    else:
        print("  empty    nothing downloaded yet; availability is provider-declared only")

    return 0 if ok else 1


def _cmd_warm(args: argparse.Namespace) -> int:
    """Triggers each bulk provider's one-time download synchronously."""
    from .catalog import StationQuery, catalog
    from .jobs import manager

    wanted = args.provider or [p.key for p in registry.PROVIDERS if p.bulk_first_use]
    for key in wanted:
        provider = registry.get_provider(key)
        if not provider.bulk_first_use:
            print(f"{provider.label}: nothing to warm.")
            continue
        if provider.cache_is_warm():
            print(f"{provider.label}: already warm.")
            continue

        page, _ = catalog.query(StationQuery(countries=[key], limit=1))
        if page.empty:
            print(f"{provider.label}: no stations in the catalog, skipping.")
            continue

        variables = provider.declared_variables()
        gauge_id = page.iloc[0][registry.GAUGE_ID]
        print(f"{provider.label}: {provider.bulk_first_use}")
        print(f"  warming via gauge {gauge_id} ...")
        try:
            fetcher = manager.fetcher(key)
            fetcher.get_data(
                gauge_id=gauge_id,
                variable=variables[0],
                start_date="2020-01-01",
                end_date="2020-01-31",
            )
        except Exception as exc:
            print(f"  FAILED: {exc}")
            continue
        print(f"  {'warm' if provider.cache_is_warm() else 'still cold — check the logs'}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if not paths.STATIONS_PARQUET.exists():
        print("No catalog yet — building it first.\n")
        build_catalog(verbose=True)
        print()

    url = f"http://{args.host}:{args.port}"
    if not paths.FRONTEND_DIST.exists():
        print(f"! The front end is not built, so {url} will only serve the API.")
        print(f"! Build it with: cd {paths.FRONTEND_DIR} && npm install && npm run build")
        print("! Or run the Vite dev server (npm run dev) and open http://localhost:5173\n")
    elif args.open:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"Global Water Atlas -> {url}")
    uvicorn.run("wateratlas.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="wateratlas", description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("build-catalog", help="Normalise the cached CSVs into one station index")
    subparsers.add_parser("doctor", help="Report what is ready and what is missing")

    warm = subparsers.add_parser("warm", help="Pre-fetch the bulk provider caches")
    warm.add_argument(
        "--provider",
        action="append",
        choices=[p.key for p in registry.PROVIDERS if p.bulk_first_use],
        help="Warm only this provider (repeatable). Default: all of them.",
    )

    serve = subparsers.add_parser("serve", help="Run the app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Reload on code changes")
    serve.add_argument(
        "--no-open", dest="open", action="store_false", help="Do not open a browser window"
    )
    serve.set_defaults(open=True)

    args = parser.parse_args(argv)
    handlers = {
        "build-catalog": _cmd_build_catalog,
        "doctor": _cmd_doctor,
        "warm": _cmd_warm,
        "serve": _cmd_serve,
    }
    if args.command is None:
        args = parser.parse_args(["serve"] + (argv or []))
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
