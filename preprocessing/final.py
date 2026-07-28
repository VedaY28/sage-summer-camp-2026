import pandas as pd

df = pd.read_csv("weather_data_hourly.csv")

df = (
    df.pivot(
        index=["date", "time", "meta.vsn"],
        columns="name",
        values="value"
    )
    .reset_index()
)

df.columns.name = None

df = df[
    [
        "date",
        "time",
        "meta.vsn",
        "humidity",
        "temp",
        "pressure",
        "wind direction",
        "wind speed",
        "pm25",
    ]
]

df.to_csv("weather_data_final.csv", index=False)

print(df.head())
