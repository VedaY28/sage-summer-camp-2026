#!/usr/bin/env python3
"""
build_image_csv.py  —  Sage Air environmental + image CSV builder

For each target node, pulls the past 2 weeks of:
  - image uploads        (name="upload")   -> value is a full image URL
  - environmental sensors (aqt./wxt.)       -> scalar values

Emits one row per image upload with the nearest sensor readings on the same
node, and columns: date, time, image url, temperature, humidity, pressure,
wind direction, wind speed, pm2.5.

Run with the venv that has sage-data-client + pandas:
    /home/veday28/venv/bin/python3 build_image_csv.py
"""
import datetime
import sage_data_client
import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
NODES = ["W0A0", "W0A4", "W09E", "W095", "W099"]

# Per-node geo + location metadata (lat, lon, human-readable location)
NODE_META = {
    "W0A0": {"lat":  41.777020833,  "lon": -87.609751048,  "location": "Chicago, Illinois (IL)"},
    "W0A4": {"lat":  41.701597727,  "lon": -87.995233141,  "location": "Lemont, Illinois (IL)"},
    "W09E": {"lat":  41.868021172,  "lon": -87.613417119,  "location": "Chicago, Illinois (IL)"},
    "W095": {"lat":  41.884884633495616, "lon": -87.97871741056426, "location": "Villa Park, Illinois (IL)"},
    "W099": {"lat":  42.051407767,  "lon": -87.677659396,  "location": "Chicago, Illinois (IL)"},
}

# (api measurement name, friendly column name)
SENSORS = [
    ("aqt.env.temp",        "temperature"),
    ("aqt.env.humidity",    "humidity"),
    ("aqt.env.pressure",    "pressure"),
    ("wxt.wind.direction",  "wind direction"),
    ("wxt.wind.speed",      "wind speed"),
    ("aqt.particle.pm2.5",  "pm25"),
]

DAYS_BACK = 14
OUT_CSV = "sageair_2week_image_data.csv"

# --------------------------------------------------------------------------
# Time window
# --------------------------------------------------------------------------
now = datetime.datetime.now(datetime.timezone.utc)
start_iso = (now - datetime.timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
end_iso   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
print(f"Window: {start_iso}  →  {end_iso}  ({DAYS_BACK} days, {len(NODES)} nodes)")
print()

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def query_sensor(vsn, name):
    """Return a 2-col DataFrame [timestamp, value] for one measurement on one node."""
    df = sage_data_client.query(
        start=start_iso, end=end_iso,
        filter={"vsn": vsn, "name": name},
    )
    if len(df) == 0:
        return pd.DataFrame(columns=["timestamp", "value"])
    out = df[["timestamp", "value"]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out.sort_values("timestamp").reset_index(drop=True)


def query_uploads(vsn):
    """Return DataFrame of upload records (timestamp, value=url, camera).

    Only image uploads (.jpg) are kept — the `name="upload"` stream also
    carries .csv weather dumps, .flac audio, .zip/.ghg data packages, etc.,
    which are not camera images.
    """
    df = sage_data_client.query(
        start=start_iso, end=end_iso,
        filter={"vsn": vsn, "name": "upload"},
    )
    if len(df) == 0:
        return pd.DataFrame(columns=["timestamp", "image url", "camera"])
    urls = df["value"].astype(str)
    is_jpg = urls.str.lower().str.endswith(".jpg")
    df = df[is_jpg].copy()
    if len(df) == 0:
        return pd.DataFrame(columns=["timestamp", "image url", "camera"])
    out = pd.DataFrame({
        "timestamp": pd.to_datetime(df["timestamp"], utc=True),
        "image url": df["value"].astype(str),
        "camera":   df.get("meta.camera", pd.Series(index=df.index)).astype(str),
    })
    return out.sort_values("timestamp").reset_index(drop=True)


def nearest_value(sensor_df, target_ts):
    """Return the nearest sensor value to target_ts, or NaN if no data."""
    if sensor_df.empty:
        return float("nan")
    deltas = (sensor_df["timestamp"] - target_ts).abs()
    idx = deltas.idxmin()
    return sensor_df.at[idx, "value"]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
all_rows = []

for vsn in NODES:
    print(f"--- {vsn} ---")

    # pull all sensor measurements for this node into a dict of DataFrames
    sensor_dfs = {}
    for api_name, col_name in SENSORS:
        sdf = query_sensor(vsn, api_name)
        sensor_dfs[col_name] = sdf
        print(f"  {col_name:14s}: {len(sdf):5d} samples")

    # pull image uploads
    uploads = query_uploads(vsn)
    print(f"  image uploads : {len(uploads):5d} samples")

    if uploads.empty:
        print(f"  (no uploads — skipping)\n")
        continue

    # for each upload, find nearest sensor reading for each measurement
    sensor_ts = {col: sdf["timestamp"].values for col, sdf in sensor_dfs.items() if not sdf.empty}
    sensor_vals = {col: sdf["value"].values     for col, sdf in sensor_dfs.items() if not sdf.empty}
    sensor_idx = {col: 0                       for col in sensor_dfs if not sensor_dfs[col].empty}

    # vectorized nearest: since everything's sorted by timestamp we can do merge_asof
    upload_df = uploads.copy()

    for col_name in [s[1] for s in SENSORS]:
        sdf = sensor_dfs[col_name]
        if sdf.empty:
            upload_df[col_name] = float("nan")
            continue
        # merge_asof: for each upload timestamp, find the nearest (backward or forward) sensor
        merged = pd.merge_asof(
            upload_df[["timestamp"]],
            sdf.rename(columns={"value": col_name}),
            on="timestamp",
            direction="nearest",
        )
        upload_df[col_name] = merged[col_name].values

    # tag every row with its node VSN so we can join geo metadata later
    upload_df["node"] = vsn

    all_rows.append(upload_df)
    print(f"  matched {len(upload_df)} upload→sensor rows\n")

# --------------------------------------------------------------------------
# Combine + format + write
# --------------------------------------------------------------------------
result = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

# split timestamp into date + time columns
result["date"] = result["timestamp"].dt.strftime("%Y-%m-%d")
result["time"] = result["timestamp"].dt.strftime("%H:%M:%S")
result = result.drop(columns=["timestamp"])

# attach per-node lat / lon / location
result["lat"]      = result["node"].map(lambda v: NODE_META[v]["lat"])
result["long"]     = result["node"].map(lambda v: NODE_META[v]["lon"])
result["location"] = result["node"].map(lambda v: NODE_META[v]["location"])

# final column order
final_cols = ["date", "time", "node", "lat", "long", "location", "image url",
              "temperature", "humidity", "pressure",
              "wind direction", "wind speed", "pm25"]
result = result[[c for c in final_cols if c in result.columns]]

result.to_csv(OUT_CSV, index=False)

print(f"✓ wrote {len(result)} rows → {OUT_CSV}")
print(f"  nodes: {', '.join(NODES)}")
print()
print("first 3 rows:")
print(result.head(3).to_string())
print()
print("last 3 rows:")
print(result.tail(3).to_string())
