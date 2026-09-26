"""Downloads discharge for a Thai gauge and recent rainfall for a Thai rain gauge.

Station M.7 (id 2752) sits on the Mun at Ubon Ratchathani and measures the
Mekong's largest Thai tributary before it joins the mainstem at Khong Chiam.
"""

import logging

import matplotlib.pyplot as plt

from rivretrieve import ThailandFetcher, ThailandRainFetcher, constants

logging.basicConfig(level=logging.INFO)

gauge_id = "2752"  # M.7, Mun River at Ubon Ratchathani
rain_gauge_id = "2005"
variable = constants.DISCHARGE_DAILY_MEAN
# The telemetry archive only becomes dense from 2023; asking for 1950 returns nothing.
start_date = "2023-01-01"
end_date = None

fetcher = ThailandFetcher()
sites = ThailandFetcher.get_cached_metadata()
station = sites.loc[gauge_id]
print(f"{station['station_code']} — {station['station_name']} ({station['province']}, {station['agency']})")

print(f"Fetching {variable} for {gauge_id} from {start_date} ...")
data = fetcher.get_data(gauge_id=gauge_id, variable=variable, start_date=start_date, end_date=end_date)

fig, axes = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1])

if not data.empty:
    print(data.head())
    print(f"Time series from {data.index.min()} to {data.index.max()} ({len(data)} days)")
    axes[0].plot(data.index, data[variable], label=f"{station['station_code']} ({gauge_id})", linewidth=1)
else:
    print(f"No data found for {gauge_id}")

axes[0].set_xlabel(constants.TIME_INDEX)
axes[0].set_ylabel(f"{variable} (m3/s)")
axes[0].set_title(f"Mun River at {station['station_code']} — daily mean discharge")
axes[0].legend()
axes[0].grid(True, alpha=0.3)

# Rainfall: the provider serves only a rolling window of roughly the last 41 hours.
rain_fetcher = ThailandRainFetcher()
rain_sites = ThailandRainFetcher.get_cached_metadata()
rain_station = rain_sites.loc[rain_gauge_id]
print(f"\nFetching recent rainfall for {rain_gauge_id} — {rain_station['station_name']} ...")
rain = rain_fetcher.get_data(gauge_id=rain_gauge_id, variable=constants.PRECIPITATION_HOURLY_SUM)

if not rain.empty:
    print(rain.tail())
    axes[1].bar(rain.index, rain[constants.PRECIPITATION_HOURLY_SUM], width=0.03, color="#3b7dd8")
else:
    print("No recent rainfall returned (the window is only the last ~41 hours).")

axes[1].set_ylabel("rainfall (mm/h)")
axes[1].set_title(f"{rain_station['station_name']} — last ~41 hours of rainfall")
axes[1].grid(True, alpha=0.3)

plt.tight_layout()
plot_path = "thailand_discharge_plot.png"
plt.savefig(plot_path)
print(f"Plot saved to {plot_path}")
