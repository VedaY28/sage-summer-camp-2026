#!/usr/bin/env python3
"""
add_raw_pm25.py — add a "raw pm25" column to sageair_2week_image_data.csv.

For each sageair row, looks up the matching node's EPA CSV (w0a0.csv, w0a4.csv,
...) by date+hour, and copies that row's "pm avg" value into "raw pm25".

Top and bottom camera rows in the same hour get the SAME raw pm25 value, since
the EPA average is per-hour, not per-camera.

Matches on (node, date, hour):
  - sageair time "23:00:09" -> floored to hour "23:00:00"
  - EPA "DateTime" is already on the hour ("23:00:00")

Rows with no EPA match (e.g. before EPA coverage starts) get a blank raw pm25.

Final column order:
  date, time, node, lat, long, location, image url, temperature,
  humidity, pressure, wind direction, wind speed, pm25, "raw pm25"

Run with:
    /home/veday28/venv/bin/python3 add_raw_pm25.py
"""
import pandas as pd

NODE_FILES = {
    "W0A0": "w0a0.csv",
    "W0A4": "w0a4.csv",
    "W09E": "w09e.csv",
    "W095": "w095.csv",
    "W099": "w099.csv",
}

# Read main sageair file
df = pd.read_csv("sageair_2week_image_data.csv")

# Build a date+hour key for matching
df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
df["hour_key"] = df["datetime"].dt.floor("h")

# Load each EPA csv, build a lookup Series indexed by (node, hour_key) -> pm avg
lookup = {}
for vsn, fname in NODE_FILES.items():
    epa = pd.read_csv(fname)
    epa["hour_key"] = pd.to_datetime(epa["DateTime"])
    epa = epa[["hour_key", "pm avg"]].dropna(subset=["hour_key"]).drop_duplicates("hour_key")
    lookup[vsn] = epa.set_index("hour_key")["pm avg"]

# Assign raw pm25 by joining node + hour_key
def get_raw(row):
    s = lookup.get(row["node"])
    if s is None:
        return float("nan")
    hk = row["hour_key"]
    return s.get(hk, float("nan"))

df["raw pm25"] = df.apply(get_raw, axis=1)

# Drop helpers
df = df.drop(columns=["datetime", "hour_key"])

# Final column order
final_cols = [
    "date", "time", "node", "lat", "long", "location", "image url",
    "temperature", "humidity", "pressure",
    "wind direction", "wind speed", "pm25", "raw pm25"
]
df = df[final_cols]

df.to_csv("sageair_2week_image_data.csv", index=False)

# report
matched = df["raw pm25"].notna().sum()
unmatched = df["raw pm25"].isna().sum()
print(f"rows: {len(df)}")
print(f"matched with EPA pm avg: {matched}")
print(f"unmatched (NaN): {unmatched}")
print()
# per-node match breakdown
for n in NODE_FILES:
    sub = df[df.node == n]
    print(f"  {n}: {sub['raw pm25'].notna().sum()}/{len(sub)} matched")
print()
print("sample rows (first 3, with raw pm25):")
print(df[["date", "time", "node", "pm25", "raw pm25"]].head(3).to_string(index=False))
print()
print("sample W09E rows (both cameras, same hour -> same raw pm25):")
sample_w09e = df[(df.node == "W09E") & (df.date == "2026-07-11") & (df.time.str.startswith("11"))]
print(sample_w09e[["date", "time", "node", "image url", "pm25", "raw pm25"]].head(3).to_string(index=False))
