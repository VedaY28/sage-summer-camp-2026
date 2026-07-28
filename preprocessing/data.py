import sage_data_client
import pandas as pd

df = sage_data_client.query(
    start="2026-01-01T15:30:31.559Z",
    end="2026-07-22T14:30:31.559Z", 
    filter={
        "vsn": "W0A4",
        "name": "aqt.env.temp|aqt.env.humidity|aqt.env.pressure|aqt.particle.pm2.5|wxt.wind.direction|wxt.wind.speed"
    }
)

#print(df)
columns_to_remove = [
    "meta.host",
    "meta.avg_frequency",
    "meta.description",
    "meta.missing",
    "meta.job",
    "meta.zone",
    "meta.units",
    "meta.task",
    "meta.sensor",
    "meta.plugin",
    "meta.node",
]

df = df.drop(columns=columns_to_remove, errors="ignore")


df["name"] = (
    df["name"]
    .str.replace(r"^aqt\.env\.", "", regex=True)
    .str.replace(r"^aqt\.particle\.", "", regex=True)
    .str.replace(r"^wxt\.", "", regex=True)
    .str.replace("wind.direction", "wind direction")
    .str.replace("wind.speed", "wind speed")
    .str.replace("pm2.5", "pm25")
)

df["timestamp"] = pd.to_datetime(df["timestamp"])

df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
df["time"] = df["timestamp"].dt.strftime("%H:%M:%S")

df = df.drop(columns=["timestamp"])

df = df[["date", "time", "name", "value", "meta.vsn"]]


print(df.columns)
print(df)
print(df["name"].unique())

df.to_csv("weather_data.csv", index=False)


