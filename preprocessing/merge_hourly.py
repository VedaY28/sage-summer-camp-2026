import csv, collections, datetime, statistics

rows = list(csv.DictReader(open('weather_data_final.csv')))
FIELDS = ['humidity', 'temp', 'pressure', 'wind direction', 'wind speed', 'pm25']

# group by (date, hour) -- same date + same hour only
by_hour = collections.defaultdict(list)
for r in rows:
    by_hour[(r['date'], r['time'][:2])].append(r)


def to_sec(t):
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + int(s)


def to_time_str(total_sec):
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


out_rows = []
for (date, hr), group in by_hour.items():
    # first pass: collect all values per field (numeric) across all rows in this hour
    field_values = {f: [] for f in FIELDS}
    seconds = []
    vsn = group[0]['meta.vsn']  # same node within an hour
    for r in group:
        seconds.append(to_sec(r['time']))
        for f in FIELDS:
            v = r[f].strip()
            if v:
                try:
                    field_values[f].append(float(v))
                except ValueError:
                    pass

    # build merged row: average each field across the hour
    merged = {
        'date': date,
        'time': to_time_str(int(statistics.mean(seconds))),
        'meta.vsn': vsn,
    }
    for f in FIELDS:
        if field_values[f]:
            vals = field_values[f]
            if len(vals) == 1:
                merged[f] = vals[0]
            else:
                # average; keep original float precision feel
                avg = sum(vals) / len(vals)
                # round to reasonable decimals to avoid long float noise
                merged[f] = round(avg, 6)
                # strip trailing .0 for ints
                if isinstance(merged[f], float) and merged[f] == int(merged[f]):
                    merged[f] = int(merged[f])
        else:
            merged[f] = ''
    out_rows.append(merged)

# sort by date then time
out_rows.sort(key=lambda r: (r['date'], r['time']))

cols = ['date', 'time', 'meta.vsn', 'humidity', 'temp', 'pressure',
        'wind direction', 'wind speed', 'pm25']
with open('weather_data_hourly_merged.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(out_rows)

# report
print(f"input rows: {len(rows)}")
print(f"output rows (one per date+hour): {len(out_rows)}")
print(f"hours reduced: {len(rows) - len(out_rows)}")
print()
print("first 5 rows:")
for r in out_rows[:5]:
    print(r)
print()
# spot-check the example hour we discussed
for r in out_rows:
    if r['date'] == '2026-06-22' and r['time'].startswith('12'):
        print("spot-check 2026-06-22 hour 12 (was 4 rows, averaged):")
        print(" ", r)
