#!/usr/bin/env python3
"""
SageAir v6 test inference — runs the v6 ResNet50 model on the test set,
saves predictions CSV, metrics CSV, confusion matrix PNG, confidence
histogram PNG, and sample predictions grid PNG.

Usage:
  python3 v6_test_inference.py
  python3 v6_test_inference.py --sample-count 25
"""
from __future__ import annotations

import argparse
import csv
import math
import pickle
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import transforms, models
from sklearn.metrics import roc_auc_score
from matplotlib import pyplot as plt


# ── Config ─────────────────────────────────────────────────────────────
MODEL_DIR = Path("/home/veday28/SageAir/models/v6_resnet50_pa151")
MODEL_PATH = MODEL_DIR / "best_model.pt"
SCALER_PATH = MODEL_DIR / "meteo_scaler.pkl"
CSV_PATH = Path("/home/veday28/SageAir/all_data_with_weathervar.csv")
TEST_DIR = Path("/home/veday28/SageAir/images_v2/test")
OUTPUT_DIR = MODEL_DIR / "test_inference"

IMG_SIZE = 224
# ImageNet normalization (ResNet50 was pretrained on ImageNet)
IMG_MEAN = [0.485, 0.456, 0.406]
IMG_STD  = [0.229, 0.224, 0.225]
METEO_COLS = ["temp", "humidity", "pressure"]

IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


# ── Model (must match train_v6.py) ──────────────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        resnet = models.resnet50(weights=None)
        # Freeze everything except layer4
        for param in resnet.parameters():
            param.requires_grad = False
        for param in resnet.layer4.parameters():
            param.requires_grad = True
        self.image_encoder = nn.Sequential(*list(resnet.children())[:-1])
        self.img_proj = nn.Linear(2048, 512)
        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.head = nn.Sequential(
            nn.Linear(512 + 32, 256),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, img, met):
        feats = self.image_encoder(img)
        feats = feats.flatten(1)
        feats = self.img_proj(feats)
        met_feats = self.meteo_encoder(met)
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)


# ── Eval transform (no augmentation) ────────────────────────────────────
eval_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=IMG_MEAN, std=IMG_STD),
])


# ── Helpers ────────────────────────────────────────────────────────────
def collect_samples(data_dir: Path) -> list[tuple[Path, str]]:
    samples: list[tuple[Path, str]] = []
    for class_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        for img_path in sorted(class_dir.rglob("*")):
            if img_path.is_file() and img_path.suffix.lower() in IMAGE_SUFFIXES:
                samples.append((img_path, class_dir.name))
    if not samples:
        raise ValueError(f"No images found under: {data_dir}")
    return samples


def batch_items(items: list, batch_size: int) -> Iterable[list]:
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def class_metrics(labels, actual, predicted):
    metrics = {}
    for label in labels:
        tp = sum(t == label and p == label for t, p in zip(actual, predicted))
        fp = sum(t != label and p == label for t, p in zip(actual, predicted))
        fn = sum(t == label and p != label for t, p in zip(actual, predicted))
        support = sum(t == label for t in actual)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        metrics[label] = (precision, recall, f1, support)
    return metrics


def write_predictions_csv(output_path, predictions):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("image", "ground_truth", "prediction", "confidence", "prob_good", "prob_bad", "correct"))
        for path, truth, pred, conf, pg, pb in predictions:
            w.writerow((path.as_posix(), truth, pred, f"{conf:.6f}", f"{pg:.6f}", f"{pb:.6f}", pred == truth))


def write_metrics_csv(output_path, labels, metrics, accuracy, macro, total):
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("class", "precision", "recall", "f1", "support"))
        for label in labels:
            p, r, f1, s = metrics[label]
            w.writerow((label, f"{p:.6f}", f"{r:.6f}", f"{f1:.6f}", s))
        w.writerow(())
        w.writerow(("accuracy", f"{accuracy:.6f}", "", "", total))
        w.writerow(("macro_avg", f"{macro[0]:.6f}", f"{macro[1]:.6f}", f"{macro[2]:.6f}", total))


def plot_confusion_matrix(output_path, labels, confusion):
    matrix = [[confusion[truth, pred] for pred in labels] for truth in labels]
    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 1.5), max(5, len(labels) * 1.25)))
    im = ax.imshow(matrix, cmap="Blues")
    fig.colorbar(im, ax=ax, label="Images")
    ax.set_xticks(range(len(labels)), labels=labels, rotation=45, ha="right")
    ax.set_yticks(range(len(labels)), labels=labels)
    ax.set_xlabel("Prediction")
    ax.set_ylabel("Ground truth")
    ax.set_title("Multimodal — Confusion Matrix")
    threshold = max((v for row in matrix for v in row), default=0) / 2
    for ri, row in enumerate(matrix):
        for ci, val in enumerate(row):
            color = "white" if val > threshold else "black"
            ax.text(ci, ri, str(val), ha="center", va="center", color=color)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_confidence_histogram(output_path, predictions):
    correct_conf = [c for _, t, p, c, _, _ in predictions if p == t]
    wrong_conf = [c for _, t, p, c, _, _ in predictions if p != t]
    fig, ax = plt.subplots(figsize=(8, 5))
    bins = [i / 20 for i in range(21)]
    ax.hist([correct_conf, wrong_conf], bins=bins, stacked=True,
            label=["correct", "incorrect"], color=["#2ca02c", "#d62728"])
    ax.set_xlabel("Top-1 confidence")
    ax.set_ylabel("Images")
    ax.set_title("SageAir v6 — Prediction Confidence Distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_prediction_grid(output_path, predictions, sample_count):
    if sample_count < 1 or not predictions:
        return
    wrong = [item for item in predictions if item[2] != item[1]]
    right = [item for item in predictions if item[2] == item[1]]
    selection = []
    selection.extend(wrong[:sample_count])
    if len(selection) < sample_count:
        selection.extend(right[:sample_count - len(selection)])

    cols = min(5, len(selection))
    rows = math.ceil(len(selection) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.6, rows * 2.8))
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]
    for idx, ax in enumerate(axes):
        ax.axis("off")
        if idx >= len(selection):
            continue
        path, truth, pred, conf, _, _ = selection[idx]
        try:
            with Image.open(path) as raw:
                ax.imshow(raw.convert("RGB"))
        except OSError:
            continue
        correct = pred == truth
        ax.set_title(
            f"gt: {truth}\npred: {pred} ({conf:.2f})",
            fontsize=8, color="green" if correct else "red"
        )
    fig.suptitle("Multimodal — Sample Predictions")
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


# ── Main ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SageAir v6 test inference + visualizations")
    parser.add_argument("--sample-count", type=int, default=25, help="Number of sample predictions in grid")
    parser.add_argument("--batch", type=int, default=32, help="Inference batch size")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Load CSV meteo lookup
    import pandas as pd
    df = pd.read_csv(CSV_PATH)
    df["filename"] = df["filename"].str.strip()
    meteo_lookup = df.set_index("filename")[METEO_COLS].to_dict("index")
    print(f"Meteo lookup: {len(meteo_lookup)} entries")

    # Load scaler
    with open(SCALER_PATH, "rb") as f:
        scaler = pickle.load(f)

    # Load model
    model = AirQualityModel(meteo_dim=3, dropout=0.3).to(device)
    ckpt = torch.load(MODEL_PATH, weights_only=True, map_location=device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    print(f"Model loaded (epoch {ckpt['epoch']}, val F1={ckpt['val_f1']:.4f})")

    # Collect test samples
    samples = collect_samples(TEST_DIR)
    print(f"Test samples: {len(samples)}")

    # Run inference
    predictions = []
    for chunk in batch_items(samples, args.batch):
        paths, truths = zip(*chunk)
        imgs = []
        mets = []
        for p, _ in zip(paths, truths):
            img = Image.open(p).convert("RGB")
            imgs.append(eval_transform(img))
            row = meteo_lookup[p.name]
            raw = np.array([[row["temp"], row["humidity"], row["pressure"]]])
            mets.append(torch.tensor(scaler.transform(raw)[0], dtype=torch.float32))
        img_batch = torch.stack(imgs).to(device)
        met_batch = torch.stack(mets).to(device)
        with torch.no_grad():
            outputs = model(img_batch, met_batch)
            probs = torch.softmax(outputs, dim=1)
        for p, truth, prob in zip(paths, truths, probs):
            pred_idx = prob.argmax().item()
            pred = "bad" if pred_idx == 1 else "good"
            conf = float(prob[pred_idx])
            pg = float(prob[0])
            pb = float(prob[1])
            predictions.append((p, truth, pred, conf, pg, pb))

    # Compute metrics
    actual = [t for _, t, _, _, _, _ in predictions]
    predicted = [p for _, _, p, _, _, _ in predictions]
    labels = sorted(set(actual) | set(predicted))
    metrics = class_metrics(labels, actual, predicted)
    correct = sum(t == p for t, p in zip(actual, predicted))
    accuracy = correct / len(actual)
    macro_p = sum(m[0] for m in metrics.values()) / len(metrics)
    macro_r = sum(m[1] for m in metrics.values()) / len(metrics)
    macro_f1 = sum(m[2] for m in metrics.values()) / len(metrics)
    confusion = Counter(zip(actual, predicted))

    # AUC
    prob_bad_list = [pb for _, _, _, _, _, pb in predictions]
    actual_binary = [1 if t == "bad" else 0 for t in actual]
    try:
        auc = roc_auc_score(actual_binary, prob_bad_list)
    except Exception:
        auc = 0.0

    # Write outputs
    write_predictions_csv(OUTPUT_DIR / "test_predictions.csv", predictions)
    write_metrics_csv(OUTPUT_DIR / "test_metrics.csv", labels, metrics, accuracy,
                       (macro_p, macro_r, macro_f1), len(actual))
    plot_confusion_matrix(OUTPUT_DIR / "confusion_matrix.png", labels, confusion)
    plot_confidence_histogram(OUTPUT_DIR / "confidence_histogram.png", predictions)
    plot_prediction_grid(OUTPUT_DIR / "sample_predictions.png", predictions, args.sample_count)

    # Print summary
    print(f"\n{'='*50}")
    print(f"  Test samples: {len(actual)}")
    print(f"  Accuracy:     {accuracy:.4f} ({correct}/{len(actual)})")
    print(f"  Macro F1:     {macro_f1:.4f}")
    print(f"  AUC:          {auc:.4f}")
    print(f"{'='*50}")
    print(f"\nPer-class:")
    print(f"{'class':<10} {'precision':>10} {'recall':>10} {'f1':>10} {'support':>10}")
    for label in labels:
        p, r, f1, s = metrics[label]
        print(f"{label:<10} {p:>10.4f} {r:>10.4f} {f1:>10.4f} {s:>10}")
    print(f"\nConfusion matrix:")
    print(f"  True\\Pred  {'  '.join(labels)}")
    for truth in labels:
        row = [confusion[truth, pred] for pred in labels]
        print(f"  {truth:<8}  {row[0]:>4}  {row[1]:>4}")

    print(f"\nSaved to {OUTPUT_DIR}/")
    print(f"  - test_predictions.csv")
    print(f"  - test_metrics.csv")
    print(f"  - confusion_matrix.png")
    print(f"  - confidence_histogram.png")
    print(f"  - sample_predictions.png")


if __name__ == "__main__":
    main()
