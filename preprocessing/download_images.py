#!/usr/bin/env python3
"""Download all images from sageair_2week_image_data.csv into images/ cache."""
import pandas as pd
from pathlib import Path
import hashlib
import subprocess
import concurrent.futures
import time
import sys
import json

CSV_PATH = "/home/veday28/SageAir/sageair_2week_image_data.csv"
IMG_DIR = Path("/home/veday28/SageAir/images")
FAIL_LOG = Path("/home/veday28/SageAir/download_failures.json")
PROGRESS_FILE = Path("/home/veday28/SageAir/download_progress.json")

# Load token
ENV_PATH = Path("/home/veday28/.hermes/profiles/sage/.env")
PORTAL_USER = "veday28"
token = None
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if line.startswith("SAGE_PORTAL_TOKEN=") and not line.startswith("#"):
        token = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
if not token:
    print("ERROR: SAGE_PORTAL_TOKEN not found in .env")
    sys.exit(1)

IMG_DIR.mkdir(parents=True, exist_ok=True)

# Load CSV
df = pd.read_csv(CSV_PATH)
urls = df["image url"].tolist()
print(f"Total URLs to download: {len(urls)}")

# Check which are already downloaded
already = set()
for f in IMG_DIR.glob("*.jpg"):
    already.add(f.stem)
print(f"Already downloaded: {len(already)}")

# Build work list: (url, filename, idx)
work = []
for i, url in enumerate(urls):
    fname = hashlib.sha1(url.encode()).hexdigest()[:16]
    if fname not in already:
        work.append((url, fname, i))
print(f"To download: {len(work)}")


def download_one(args):
    url, fname, idx = args
    out_path = IMG_DIR / f"{fname}.jpg"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-u", f"{PORTAL_USER}:{token}",
             "-o", str(out_path), "-w", "%{http_code}",
             "--connect-timeout", "30", "--max-time", "120", url],
            capture_output=True, text=True, timeout=150
        )
        http_code = result.stdout.strip() if result.stdout else "000"

        if http_code == "200" and out_path.exists() and out_path.stat().st_size > 100:
            return {"idx": idx, "fname": fname, "status": "ok", "size": out_path.stat().st_size}
        else:
            # Clean up partial/failed file
            if out_path.exists() and out_path.stat().st_size < 100:
                out_path.unlink()
            return {"idx": idx, "fname": fname, "status": "fail", "http_code": http_code,
                    "size": out_path.stat().st_size if out_path.exists() else 0}
    except Exception as e:
        if out_path.exists() and out_path.stat().st_size < 100:
            out_path.unlink()
        return {"idx": idx, "fname": fname, "status": "error", "error": str(e)}


MAX_WORKERS = 8
BATCH_SIZE = 200

total_done = len(already)
total_fail = 0
failures = []

t0 = time.time()

for batch_start in range(0, len(work), BATCH_SIZE):
    batch = work[batch_start:batch_start + BATCH_SIZE]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(download_one, batch))

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] != "ok")
    total_done += ok
    total_fail += fail

    for r in results:
        if r["status"] != "ok":
            failures.append(r)

    elapsed = time.time() - t0
    rate = (total_done - len(already)) / elapsed if elapsed > 0 else 0
    remaining = len(work) - (batch_start + len(batch))
    eta = remaining / rate if rate > 0 else 0

    print(f"[{total_done}/{len(urls)}] batch {batch_start//BATCH_SIZE + 1}: "
          f"+{ok} ok, +{fail} fail | "
          f"{rate:.1f} img/s | ETA {eta:.0f}s | fails={total_fail}")

    # Write progress periodically
    PROGRESS_FILE.write_text(json.dumps({
        "total": len(urls), "done": total_done, "failed": total_fail,
        "remaining": len(urls) - total_done
    }))

# Write failure log
if failures:
    FAIL_LOG.write_text(json.dumps(failures, indent=2))
    print(f"\n{len(failures)} failures logged to {FAIL_LOG}")
else:
    print("\nNo failures!")

# Final summary
elapsed = time.time() - t0
print(f"\n=== Download complete ===")
print(f"Total images: {len(urls)}")
print(f"Already had: {len(already)}")
print(f"Downloaded: {total_done - len(already)}")
print(f"Failed: {total_fail}")
print(f"Cached: {total_done}/{len(urls)}")
print(f"Time: {elapsed:.1f}s")

# Check disk usage
du = subprocess.run(["du", "-sh", str(IMG_DIR)], capture_output=True, text=True)
print(f"Disk: {du.stdout.strip()}")
