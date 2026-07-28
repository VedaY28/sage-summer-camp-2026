import csv, collections

rows = list(csv.DictReader(open('weather_data_final.csv')))
by_hour = collections.defaultdict(list)
for r in rows:
    by_hour[(r['date'], r['time'][:2])].append(r)

FIELDS = ['humidity', 'temp', 'pressure', 'wind direction', 'wind speed', 'pm25']

print("=== hours with CONFLICTING overlapping field values ===")
for k, g in sorted(by_hour.items()):
    seen = {}
    conflict = False
    for r in g:
        for f in FIELDS:
            if r[f]:
                if f in seen and seen[f] != r[f]:
                    conflict = True
                seen.setdefault(f, r[f])
    if conflict:
        print(f"\n{k} ({len(g)} rows):")
        for r in sorted(g, key=lambda r: r['time']):
            print(f"  {r['time']}: h={r['humidity']!r} t={r['temp']!r} p={r['pressure']!r} wd={r['wind direction']!r} ws={r['wind speed']!r} pm={r['pm25']!r}")

print("\n=== hours with MORE THAN 2 rows ===")
for k, g in sorted(by_hour.items()):
    if len(g) > 2:
        print(f"\n{k} ({len(g)} rows):")
        for r in sorted(g, key=lambda r: r['time']):
            print(f"  {r['time']}: h={r['humidity']!r} t={r['temp']!r} p={r['pressure']!r} wd={r['wind direction']!r} ws={r['wind speed']!r} pm={r['pm25']!r}")
