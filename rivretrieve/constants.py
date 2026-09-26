"""Module for global constants."""

# Index.
GAUGE_ID = "gauge_id"
TIME_INDEX = "time"

# Attributes.
ALTITUDE = "altitude"
AREA = "area"
COUNTRY = "country"
LATITUDE = "latitude"
LONGITUDE = "longitude"
RIVER = "river"
SOURCE = "source"
STATION_NAME = "station_name"

# General list of variables.
DISCHARGE = "discharge"
STAGE = "stage"
_WATER_TEMPERATURE = "water-temperature"
_CATCHMENT_PRECIPITATION = "catchment-precipitation"
# Rain measured at a single gauge, as opposed to a catchment average.
_PRECIPITATION = "precipitation"

# List of temporal resolutions.
DAILY = "daily"
HOURLY = "hourly"
INSTANTANEOUS = "instantaneous"
MONTHLY = "monthly"

# List of temporal aggregrations.
_MEAN = "mean"
_MIN = "min"
_MAX = "max"
_SUM = "sum"

# ------------------------------ Supported set of variables ----------------------------------------

# Discharge.
DISCHARGE_DAILY_MEAN = f"{DISCHARGE}_{DAILY}_{_MEAN}"
DISCHARGE_DAILY_MAX = f"{DISCHARGE}_{DAILY}_{_MAX}"
DISCHARGE_DAILY_MIN = f"{DISCHARGE}_{DAILY}_{_MIN}"
DISCHARGE_HOURLY_MEAN = f"{DISCHARGE}_{HOURLY}_{_MEAN}"
DISCHARGE_INSTANT = f"{DISCHARGE}_{INSTANTANEOUS}"
DISCHARGE_MONTHLY_MEAN = f"{DISCHARGE}_{MONTHLY}_{_MEAN}"
DISCHARGE_MONTHLY_MAX = f"{DISCHARGE}_{MONTHLY}_{_MIN}"
DISCHARGE_MONTHLY_MIN = f"{DISCHARGE}_{MONTHLY}_{_MAX}"

# Stage.
STAGE_DAILY_MEAN = f"{STAGE}_{DAILY}_{_MEAN}"
STAGE_DAILY_MAX = f"{STAGE}_{DAILY}_{_MAX}"
STAGE_DAILY_MIN = f"{STAGE}_{DAILY}_{_MIN}"
STAGE_HOURLY_MEAN = f"{STAGE}_{HOURLY}_{_MEAN}"
STAGE_INSTANT = f"{STAGE}_{INSTANTANEOUS}"
STAGE_MONTHLY_MEAN = f"{STAGE}_{MONTHLY}_{_MEAN}"
STAGE_MONTHLY_MAX = f"{STAGE}_{MONTHLY}_{_MIN}"
STAGE_MONTHLY_MIN = f"{STAGE}_{MONTHLY}_{_MAX}"

# Water temperature.
WATER_TEMPERATURE_DAILY_MEAN = f"{_WATER_TEMPERATURE}_{DAILY}_{_MEAN}"
WATER_TEMPERATURE_DAILY_MAX = f"{_WATER_TEMPERATURE}_{DAILY}_{_MAX}"
WATER_TEMPERATURE_DAILY_MIN = f"{_WATER_TEMPERATURE}_{DAILY}_{_MIN}"
WATER_TEMPERATURE_HOURLY_MEAN = f"{_WATER_TEMPERATURE}_{HOURLY}_{_MEAN}"
WATER_TEMPERATURE_INSTANT = f"{_WATER_TEMPERATURE}_{INSTANTANEOUS}"
WATER_TEMPERATURE_MONTHLY_MEAN = f"{_WATER_TEMPERATURE}_{MONTHLY}_{_MEAN}"

# Precipitation.
# ``CATCHMENT_PRECIPITATION_*`` is an areal average over a catchment;
# ``PRECIPITATION_*`` is what a single rain gauge measured. Keeping them apart
# matters: a national rain-gauge network is not a set of catchment averages.
CATCHMENT_PRECIPITATION_DAILY_SUM = f"{_CATCHMENT_PRECIPITATION}_{DAILY}_{_SUM}"
PRECIPITATION_HOURLY_SUM = f"{_PRECIPITATION}_{HOURLY}_{_SUM}"
PRECIPITATION_DAILY_SUM = f"{_PRECIPITATION}_{DAILY}_{_SUM}"
