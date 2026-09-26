"""Global Water Atlas — a local-first map of observed water data from the RivRetrieve library.

Rivers, rainfall, reservoirs, water temperature and coastal and Great Lakes
water levels, from every provider RivRetrieve has a fetcher for.

This package is a *consumer* of ``rivretrieve``. It never modifies the library:
every station list and every time series still comes from the fetchers in
``rivretrieve/``.
"""

__version__ = "0.1.0"
