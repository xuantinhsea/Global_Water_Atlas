import logging

import matplotlib.pyplot as plt

from rivretrieve import NOAATidesFetcher, constants

logging.basicConfig(level=logging.INFO)

# The Battery (New York Harbor) and Carrollton on the Mississippi at New Orleans.
gauge_ids = [
    "8518750",
    "8761955",
]
variable = constants.STAGE_DAILY_MEAN
# One request per year of daily data, so keep the example short.
start_date = "2023-01-01"
end_date = "2024-12-31"

plt.figure(figsize=(12, 6))

fetcher = NOAATidesFetcher()
for gauge_id in gauge_ids:
    print(f"Fetching data for {gauge_id} from {start_date} to {end_date}...")
    data = fetcher.get_data(gauge_id=gauge_id, variable=variable, start_date=start_date, end_date=end_date)
    if not data.empty:
        datum = data.attrs.get("datum")
        print(f"Data for {gauge_id} (metres above {datum}):")
        print(data.head())
        print(f"Time series from {data.index.min()} to {data.index.max()}")
        plt.plot(data.index, data[variable], label=f"{gauge_id} ({datum})")
    else:
        print(f"No data found for {gauge_id}")

plt.xlabel(constants.TIME_INDEX)
plt.ylabel(f"{variable} (m above station datum)")
plt.title("NOAA Tides & Currents daily mean water level")
plt.legend()
plt.grid(True)
plt.tight_layout()
plot_path = "noaa_tides_stage_plot.png"
plt.savefig(plot_path)
print(f"Plot saved to {plot_path}")
