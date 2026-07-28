import csv, collections

rows = list(csv.DictReader(open('weather_data_final.csv')))
FIELDS = ['humidity', 'temp', 'pressure', 'wind direction', 'wind speed', 'pm25']
by_minute = collections.defaultdict(list)
for r in rows:
    by_minute[(r['date'], r['time'][:5])].append(r)


def to_sec(t):
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + int(s)


two_apart_complement = 0
two_apart_overlap = 0
far_apart_complement = 0
far_apart_overlap = 0
sample_good = []
sample_bad = []

for k, g in by_minute.items():
    if len(g) != 2:
        continue
    a, b = sorted(g, key=lambda r: r['time'])
    delta = to_sec(b['time']) - to_sec(a['time'])
    a_present = set(f for f in FIELDS if a[f])
    b_present = set(f for f in FIELDS if b[f])
    overlap = a_present & b_present
    union = a_present | b_present
    is_complement = (len(overlap) == 0) and (union == set(FIELDS))
    if delta == 2:
        if is_complement:
            two_apart_complement += 1
            if len(sample_good) < 3:
                sample_good.append((k, a['time'], b['time'], sorted(a_present), sorted(b_present)))
        else:
            two_apart_overlap += 1
            if len(sample_bad) < 3:
                sample_bad.append((k, a['time'], b['time'], sorted(a_present), sorted(b_present), sorted(overlap)))
    else:
        if is_complement:
            far_apart_complement += 1
        else:
            far_apart_overlap += 1

print('2s apart + complementary (clean merge):', two_apart_complement)
print('2s apart + NOT complementary (overlap):', two_apart_overlap)
print('far apart + complementary:', far_apart_complement)
print('far apart + NOT complementary:', far_apart_overlap)
print('Good samples:', sample_good)
print('Bad samples:', sample_bad)
