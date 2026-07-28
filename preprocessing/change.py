import pandas as pd

# Read the CSV
df = pd.read_csv("weather_data.csv")

# Combine date and time into a datetime
df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])

# Round each timestamp to the nearest hour
df["nearest_hour"] = df["datetime"].dt.round("h")

# Calculate distance from the nearest hour
df["distance"] = (df["datetime"] - df["nearest_hour"]).abs()

# Keep the closest reading to each hour for each sensor
df = (
    df.sort_values("distance")
      .groupby(["nearest_hour", "name"], as_index=False)
      .first()
)

# Restore date and time columns
df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
df["time"] = df["datetime"].dt.strftime("%H:%M:%S")

# Keep only the desired columns
df = df[["date", "time", "name", "value", "meta.vsn"]]

# Save the filtered data
df.to_csv("weather_data_hourly.csv", index=False)

print(df.head())
