#!/usr/bin/env python3
"""
add_pm_avg.py — add a "pm avg" column to each node's EPA PM2.5 CSV.

For each row, averages the two PM2.5 sensor columns for that node and writes
the result into a new "pm avg" column. The original "Average" column is left
untouched (it's a daily average filled only on the first row of each day).

  w0a0.csv  -> Sweet Water Foundation A + B
  w0a4.csv  -> WWJ-131a A + B  (Burr Ridge A/B are dropped from the avg)
  w09e.csv  -> ODE A + B
  w095.csv  -> Elmhurst - Euclid A + B
  w099.csv  -> McCleason Manor A + B

Run with pandas:
    /home/veday28/venv/bin/python3 add_pm_avg.py
"""
import pandas as pd

# (filename, list of PM2.5 columns to average)
FILES = [
    ("w0a0.csv", ["Sweet Water Foundation A", "Sweet Water Foundation B"]),
    ("w0a4.csv", ["WWJ-131a A", "WWJ-131a B"]),
    ("w09e.csv", ["ODE A", "ODE B"]),
    ("w095.csv", ["Elmhurst - Euclid A", "Elmhurst - Euclid B"]),
    ("w099.csv", ["McCleason Manor A", "McCleason Manor B"]),
]

for fname, cols in FILES:
    df = pd.read_csv(fname)
    # compute mean across the two sensor columns (skips NaN by default)
    df["pm avg"] = df[cols].mean(axis=1)
    df.to_csv(fname, index=False)
    print(f"{fname}: added 'pm avg' from {cols}")
    print(df[["DateTime", *cols, "pm avg"]].head(3).to_string(index=False))
    print()
