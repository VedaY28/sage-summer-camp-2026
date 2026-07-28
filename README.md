# sage-summer-camp-2026

This repository contains data and code for the SAGE Summer Camp 2026 project, which builds a **multimodal air-quality classifier** that predicts whether outdoor air is good or bad from a camera image combined with temperature, humidity, and pressure readings. The model is trained on 2 weeks of data from five SAGE edge nodes in the Chicago area, using PurpleAir PM2.5 as the ground-truth label.

## Time window

All data was collected for the **14 days prior to 2026-07-24 17:00 UTC**, going back to 2026-07-10. This window was chosen because Chicago experienced historically bad air pollution on **July 16, 2026**. The 14-day window was also the maximum historical range available for direct data download from PurpleAir, because the API was not working reliably at the time of collection.

## SAGE node selection

SAGE nodes were selected using a single practical criterion: they needed to have image data for the previous two weeks, and we needed access to them. We were only granted access to the **NIREM** nodes. The five nodes used in this project are:

- `W0A0` — Sweet Water Foundation, Chicago, IL
- `W0A4` — WWJ-131a, Lemont, IL
- `W09E` — ODE, Chicago, IL
- `W095` — Elmhurst - Euclid, Villa Park, IL
- `W099` — McCleason Manor, Chicago, IL

## Data collection

### SAGE data

SAGE data was queried using `sage-data-client` (see `preprocessing/build_image_csv.py`):

- **Images**: `upload` events with `.jpg` URLs (imagesampler top/bottom cameras)
- **Environmental sensors**: `aqt.env.temp`, `aqt.env.humidity`, `aqt.env.pressure`
- **Wind sensors**: `wxt.wind.direction`, `wxt.wind.speed`
- **SAGE PM2.5**: `aqt.particle.pm2.5` from each node's onboard air-quality sensor

For each image upload, the nearest sensor reading on the same node is matched by timestamp using `pd.merge_asof` (direction="nearest"). The result is written to `sageair_2week_image_data.csv` — one row per image upload.

Images are then downloaded from the SAGE object store using `preprocessing/download_images.py`, which authenticates with the SAGE portal token and caches all images locally. Files are named by sha1-hashing the image URL.

### Why SAGE PM2.5 was not used as ground truth

We originally intended to use the SAGE nodes' own `pm2.5` readings, but after downloading them the data was unusable for all nodes except `W0A4`: the series were either a flat line, physically unrealistic (reporting healthy air during Chicago's worst pollution of the window), or heavily fragmented. The median SAGE AQT PM2.5 is ~0.4 µg/m³, while the median PurpleAir reading is ~65 µg/m³ — a major scale discrepancy. We instead adopted a single unified ground-truth source, **PurpleAir**.

### PurpleAir ground truth

For each SAGE node we identified the geographically closest PurpleAir station and downloaded the past 14 days of US EPA PM2.5 measurements. Raw reference CSVs live in `purple_air_raw_data/` (one per node, e.g. `w0a4.csv`).

The merge pipeline works as follows:

1. **`preprocessing/add_pm_avg.py`** — each PurpleAir station reports two sensor channels (A and B). The median (mean of the two) is taken per timestamp as a `pm avg` column to reduce single-sensor noise.
2. **`preprocessing/add_raw_pm25.py`** — for each image row in `sageair_2week_image_data.csv`, the matching node's PurpleAir CSV is looked up by `(node, date, hour)`. The hourly `pm avg` value is copied into a new `raw pm25` column. Top and bottom camera rows in the same hour receive the same PurpleAir value, since the reference is per-hour, not per-camera.

We use the EPA's air-quality categories, where any PM2.5 value **≥ 151** qualifies as "Unhealthy" (for all groups). This threshold gives an 86.7 / 13.3 good/bad split across the full dataset.

## Dataset preparation

`preprocessing/split_dataset.py` turns `all_data_with_weathervar.csv` and the downloaded images into a balanced train/val/test dataset:

- **Labels**: images are labelled `bad` when `purple_air_pm25 >= 151`, otherwise `good`.
- **Per-day stratified split**: images are grouped by `(date, label)` and each group is split **70/20/10** into train/val/test. Splitting per day guarantees that every day is represented across all three splits.
- **Class balancing**: after splitting, the majority class in each split is randomly downsampled so that every split has a **uniform 50/50 good/bad distribution**.
- **Image resize**: images are resized to 224×224 (or another size via `--imgsz`).

The resulting split has **554 train / 158 val / 84 test** images, balanced 50/50.

The committed split lives in `images_v2/` (organized as `train/{good,bad}/` and `val/`, `test/`), which is git-ignored due to size (~14 GB). The canonical CSV is `all_data_with_weathervar.csv`.

## Model training and results

We iterated through six model versions (`train_scripts/train.py` through `train_v6.py`). Early versions (v1–v4) used a cross-node split (W0A0, W09E, W099 for train; W0A4, W095 for test) and the EPA 24-hour PM2.5 threshold of 35 µg/m³. Later versions (v5–v6) moved to the per-day stratified split above and the PurpleAir threshold of 151 (the "Unhealthy" category boundary).

All models share the same multimodal architecture:

```
Image → CNN/CLIP encoder → embedding
[temp, humidity, pressure] → StandardScaler → MLP
[embedding ; meteo] → head MLP → binary (good/bad)
```

A **synthetic haze augmentation** (`AddRandomHaze`, p=0.3) overlays random fog/haze on training images to teach the model what bad air looks like visually — an important regularizer because most real "bad" images come from only a few peak-pollution hours.

### Model progression

| Version | Backbone | Threshold | Split | Test Acc | Test F1 | Test AUC | Notes |
|---------|----------|-----------|-------|----------|---------|----------|-------|
| v1 | ResNet50 (frozen) | 35 (SAGE pm25) | cross-node | 74.2% | 0.565 | 0.796 | baseline; class imbalance hurt F1 |
| v2 | ResNet50 (layer4 fine-tuned) | 35 (SAGE pm25) | cross-node | 78.7% | 0.657 | 0.831 | fine-tuning + stronger aug + label smoothing |
| v3 | CLIP ViT-B/32 | 15 (SAGE pm25) | cross-node | 77.5% | 0.817 | 0.839 | CLIP encoder understood haze better; lower threshold balanced classes |
| v4 | CLIP ViT-B/32 | 35 (SAGE pm25) | cross-node | 82.8% | 0.648 | 0.832 | same as v3, higher threshold; smoke test failed (99.4% bad) |
| v5 | CLIP ViT-B/32 | 151 (PurpleAir) | per-day stratified | 83.3% | 0.833 | 0.950 | switched to PurpleAir ground truth + new split |
| **v6** | **ResNet50 (layer4 fine-tuned)** | **151 (PurpleAir)** | **per-day stratified** | **90.5%** | **0.907** | **0.955** | **best model** |

### Best model (v6)

`models/v6_resnet50_pa151/training_results.json`:

- **Backbone**: ResNet50 (ImageNet pretrained), layer4 fine-tuned at LR 1e-5
- **Head LR**: 5e-4
- **Batch size**: 32, **Epochs**: 200 (early stopped at epoch 28, patience 20)
- **Label smoothing**: 0.1
- **Haze augmentation**: p=0.3, max_intensity=0.5
- **Image size**: 224×224
- **Best epoch**: 28

Test-set performance from `models/v6_resnet50_pa151/test_inference/test_metrics.csv`:

| Class | Precision | Recall | F1 | Support |
|-------|-----------|--------|------|---------|
| bad | 0.886 | 0.929 | 0.907 | 42 |
| good | 0.925 | 0.881 | 0.902 | 42 |
| **accuracy** | **0.905** | | | **84** |

Because the dataset is balanced to a uniform 50/50 good/bad distribution, this accuracy is meaningful rather than an artifact of class imbalance.

A secondary run (`models/v6_resnet50_pa151_200ep`) trained the full 200 epochs and reached best epoch 39 (acc=0.881, F1=0.881, AUC=0.960) — a higher AUC but slightly lower accuracy than the 28-epoch checkpoint, suggesting mild overfitting past epoch 28.

Test inference visualizations (confusion matrix, confidence histogram, sample prediction grid) are in `models/v6_resnet50_pa151/test_inference/`. The same artifacts exist for v5.

## Edge plugin

`plugin/` packages the trained model as a Sage edge plugin:

- **`app.py`** — the plugin entry point. Captures a camera image via `waggle.data.vision.Camera`, reads temperature/humidity/pressure from the node's sensors via `waggle.plugin.Plugin`, runs the multimodal model, and publishes the prediction through pywaggle as `airquality.prediction` (0=good, 1=bad) plus `airquality.prob_good` / `airquality.prob_bad`.
- **`Dockerfile`** — `python:3.12-slim` base, installs deps from `requirements.txt`, copies `app.py` + model weights.
- **`sage.yaml`** — plugin manifest (id `sage-air-quality`, arm64, 4Gi memory).
- **`requirements.txt`** — torch, torchvision, Pillow, numpy, scikit-learn, `pywaggle==0.56.*`.
- **`jobs/sage-air-quality.yaml`** — example `pluginctl deploy` job spec. Deploy with:
  ```
  pluginctl deploy -n sage-air-quality docker.io/library/sage-air-quality:0.1.0 -- --camera camera.top --interval 600
  ```

The plugin currently bakes in the v5 CLIP model (threshold 151) in `plugin/models/`. Updating it to the v6 ResNet50 model requires replacing `best_model.pt` and swapping the model class in `app.py` from CLIP to ResNet50.

## Inference scripts

`test_inference/` contains scripts to run trained models on new images or the held-out test set:

- **`predict_v6.py`** — single-image inference with the v6 ResNet50 model. Requires `--image`, `--temp`, `--humidity`, `--pressure`.
- **`predict_v5.py`** — same for the v5 CLIP model.
- **`v6_test_inference.py`** / **`v5_test_inference.py`** — run on the full test set, output predictions CSV, metrics CSV, confusion-matrix PNG, confidence histogram PNG, and a sample-predictions grid.

Example single-image prediction:

```bash
python3 test_inference/predict_v6.py --image test_inference/20260718_0000.02.jpg --temp 25.3 --humidity 60.2 --pressure 992.0
```

## Repository structure

```
.
├── README.md
├── all_data_with_weathervar.csv            # merged image + PurpleAir PM2.5 + meteo dataset (3020 rows)
├── sageair_2week_image_data.csv            # raw SAGE per-image CSV (3909 rows, 13 cols)
├── purple_air_raw_data/                    # PurpleAir reference CSVs per node (w0a0.csv … w099.csv)
├── preprocessing/                          # data collection and dataset prep scripts
│   ├── build_image_csv.py                  # pull images + sensors from SAGE via sage-data-client
│   ├── download_images.py                  # download all images to local cache
│   ├── add_pm_avg.py                        # average PurpleAir A/B channels per node
│   ├── add_raw_pm25.py                      # join PurpleAir PM2.5 onto SAGE image rows by hour
│   ├── split_dataset.py                     # per-day stratified 70/20/10 split + class balancing
│   └── …                                     # other helper scripts (hourly merge, scanning)
├── train_scripts/                          # model training (one file per version)
│   ├── train.py                             # v1: frozen ResNet50, th=35
│   ├── train_v2.py                          # v2: fine-tuned ResNet50, th=35
│   ├── train_v3.py                          # v3: CLIP ViT-B/32 + haze aug, th=15
│   ├── train_v4.py                          # v4: CLIP + haze aug, th=35
│   ├── train_v5.py                          # v5: CLIP + haze aug, PA th=151
│   └── train_v6.py                          # v6: ResNet50 layer4 + haze aug, PA th=151 (BEST)
├── models/                                  # trained model artifacts + results
│   ├── v1_frozen/ … v6_resnet50_pa151/     # per-version: best_model.pt, meteo_scaler.pkl, training_results.json
│   └── v6_resnet50_pa151/test_inference/    # confusion matrix, confidence histogram, sample predictions
├── plugin/                                  # Sage edge plugin (Dockerfile, app.py, sage.yaml, models/)
├── test_inference/                          # single-image + test-set inference scripts
│   ├── predict_v5.py
│   ├── predict_v6.py
│   ├── v5_test_inference.py
│   └── v6_test_inference.py
├── classroom-notes.md
├── session_log.md
└── extract_session_log.py
```

Note: image caches (`images/`, `images_v2/`) and model weights (`*.pt`) are git-ignored due to size.

## Quick start

Install dependencies (torch, torchvision, open-clip-torch, scikit-learn, pandas, sage-data-client, pywaggle, matplotlib):

```bash
pip install -r plugin/requirements.txt
```

Collect SAGE data (requires `SAGE_PORTAL_TOKEN` in the environment or a `.env` file):

```bash
python3 preprocessing/build_image_csv.py
python3 preprocessing/download_images.py
```

Add PurpleAir PM2.5 ground truth:

```bash
python3 preprocessing/add_pm_avg.py      # in purple_air_raw_data/
python3 preprocessing/add_raw_pm25.py   # writes "raw pm25" column
```

Prepare a balanced train/val/test split (writes to `images_v2/{train,val,test}/{good,bad}/`):

```bash
python3 preprocessing/split_dataset.py --threshold 151 --imgsz 224
```

Train the best model:

```bash
python3 train_scripts/train_v6.py
```

Run test-set inference and generate visualizations:

```bash
python3 test_inference/v6_test_inference.py
```

Run a single-image prediction:

```bash
python3 test_inference/predict_v6.py --image <path_to_jpg> --temp 25.3 --humidity 60.2 --pressure 992.0
```
