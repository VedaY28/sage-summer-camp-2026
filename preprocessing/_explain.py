import csv, collections

rows = list(csv.DictReader(open('weather_data_final.csv')))
FIELDS = ['humidity', 'temp', 'pressure', 'wind direction', 'wind speed', 'pm25']
by_minute = collections.defaultdict(list)
for r in rows:
    by_minute[(r['date'], r['time'][:5])].append(r)


def to_sec(t):
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + int(s)


print("=== EXAMPLE A: two rows exactly 2 SECONDS apart ===")
for k, g in sorted(by_minute.items()):
    if len(g) == 2:
        a, b = sorted(g, key=lambda r: r['time'])
        if to_sec(b['time']) - to_sec(a['time']) == 2:
            print(f"date={k[0]} minute={k[1]}")
            print(f"  row1: time={a['time']}  humidity={a['humidity']!r} temp={a['temp']!r} pressure={a['pressure']!r} wind_dir={a['wind direction']!r} wind_spd={a['wind speed']!r} pm25={a['pm25']!r}")
            print(f"  row2: time={b['time']}  humidity={b['humidity']!r} temp={b['temp']!r} pressure={b['pressure']!r} wind_dir={b['wind direction']!r} wind_spd={b['wind speed']!r} pm25={b['pm25']!r}")
            break

print("\n=== EXAMPLE B: two rows in same minute but FARTHER apart ===")
shown = 0
for k, g in sorted(by_minute.items()):
    if len(g) == 2:
        a, b = sorted(g, key=lambda r: r['time'])
        delta = to_sec(b['time']) - to_sec(a['time'])
        if delta > 2 and delta <= 60:
            print(f"date={k[0]} minute={k[1]} (delta={delta}s)")
            print(f"  row1: time={a['time']}  humidity={a['humidity']!r} temp={a['temp']!r} pressure={a['pressure']!r} wind_dir={a['wind direction']!r} wind_spd={a['wind speed']!r} pm25={a['pm25']!r}")
            print(f"  row2: time={b['time']}  humidity={b['humidity']!r} temp={b['temp']!r} pressure={b['pressure']!r} wind_dir={b['wind direction']!r} wind_spd={b['wind speed']!r} pm25={b['pm25']!r}")
            shown += 1
            if shown >= 3:
                break

# distribution of deltas for all 2-row minutes
from collections import Counter
deltas = Counter()
for k, g in by_minute.items():
    if len(g) == 2:
        a, b = sorted(g, key=lambda r: r['time'])
        deltas[to_sec(b['time']) - to_sec(a['time'])] += 1
print("\n=== how far apart the two rows are (seconds), across all 2039 two-row minutes ===")
for d, c in sorted(deltas.items()):
    print(f"  {d:>3}s apart: {c} minutes")
