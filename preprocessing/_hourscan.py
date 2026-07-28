import csv, collections

rows = list(csv.DictReader(open('weather_data_final.csv')))
by_hour = collections.defaultdict(list)
for r in rows:
    by_hour[(r['date'], r['time'][:2])].append(r)

print("total rows:", len(rows))
print("total hours:", len(by_hour))
print("hours with exactly 1 row:", sum(1 for g in by_hour.values() if len(g) == 1))
print("hours with >1 row:      ", sum(1 for g in by_hour.values() if len(g) > 1))
print("hours with >2 rows:     ", sum(1 for g in by_hour.values() if len(g) > 2))
print()

FIELDS = ['humidity', 'temp', 'pressure', 'wind direction', 'wind speed', 'pm25']
# check overlap pattern across all rows in an hour
conflict_hours = 0
clean_complement_hours = 0
for k, g in by_hour.items():
    seen_present = {}
    overlaps = False
    for r in g:
        present = set(f for f in FIELDS if r[f])
        for field in present:
            if field in seen_present:
                overlaps = True
            seen_present.setdefault(field, r[field])
    if overlaps:
        conflict_hours += 1
    else:
        clean_complement_hours += 1

print("hours with overlapping field values (CONFLICT):", conflict_hours)
print("hours with non-conflicting rows (clean):        ", clean_complement_hours)

# an hour with many rows - sample
for k, g in sorted(by_hour.items()):
    if len(g) >= 4:
        print(f"\nExample hour with many rows: {k} ({len(g)} rows)")
        times = sorted(r['time'] for r in g)
        print("  times:", times[:8], "..." if len(times) > 8 else "")
        break
