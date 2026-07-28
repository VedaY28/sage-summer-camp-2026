#!/usr/bin/env python3
"""
SageAir v3 — CLIP ViT-B/32 + synthetic haze augmentation + lower threshold.

Changes from v2:
  1. CLIP ViT-B/32 image encoder (pretrained on 400M image-text pairs, understands
     outdoor scenes/haze/smoke far better than ImageNet ResNet50)
  2. Fully fine-tunable CLIP visual encoder with discriminating LR
  3. Synthetic haze/fog augmentation — random fog/haze overlays on training images
     to teach the model what bad air looks like visually
  4. Lower PM2.5 threshold (15 µg/m³) — more balanced classes (50/50 vs 74/26)
  5. Saves to models/v3_clip_haze/ (v1 + v2 preserved)
"""
import os
import sys
import json
import hashlib
import time
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
from PIL import Image, ImageFilter
import open_clip
from sklearn.metrics import (
    classification_report, confusion_matrix,
    roc_auc_score, f1_score, accuracy_score
)
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

# ── Config ──────────────────────────────────────────────────────────────
CSV_PATH = "/home/veday28/SageAir/sageair_2week_image_data.csv"
IMG_DIR = Path("/home/veday28/SageAir/images")
SAVE_DIR = Path("/home/veday28/SageAir/models/v4_clip_haze_th35")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

PM25_THRESHOLD = 35.0    # EPA 24-hr threshold (same as v1/v2)
IMG_SIZE = 224
BATCH_SIZE = 32          # Smaller batch — CLIP ViT is bigger
NUM_EPOCHS = 25
CLIP_LR = 5e-6           # Very low LR for fine-tuning CLIP
HEAD_LR = 5e-4           # Higher LR for new head
WEIGHT_DECAY = 1e-4
DROPOUT = 0.3
LABEL_SMOOTHING = 0.1
EARLY_STOP_PATIENCE = 7
SEED = 42
HAZE_PROB = 0.3          # 30% of training images get synthetic haze

TEST_NODES = ["W0A4", "W095"]
TRAIN_NODES = ["W0A0", "W09E", "W099"]
METEO_COLS = ["temperature", "humidity", "pressure"]

torch.manual_seed(SEED)
np.random.seed(SEED)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    gpu_mem = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"GPU memory: {gpu_mem:.1f} GB")
print()

# ── Load data ────────────────────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
df["label"] = (df["raw pm25"] > PM25_THRESHOLD).astype(int)

def url_to_path(url):
    fname = hashlib.sha1(str(url).encode()).hexdigest()[:16] + ".jpg"
    return IMG_DIR / fname

df["img_path"] = df["image url"].apply(url_to_path)
missing = df[~df["img_path"].apply(lambda p: p.exists())]
print(f"Total rows: {len(df)}, Missing images: {len(missing)}")
if len(missing) > 0:
    sys.exit(1)

for col in METEO_COLS:
    n_null = df[col].isna().sum()
    if n_null > 0:
        df[col] = df[col].fillna(df[col].median())

# ── Split ────────────────────────────────────────────────────────────────
test_df = df[df["node"].isin(TEST_NODES)].copy()
trainval_df = df[df["node"].isin(TRAIN_NODES)].copy()
train_df, val_df = train_test_split(
    trainval_df, test_size=0.2, stratify=trainval_df["label"], random_state=SEED
)

print(f"\n=== Split (threshold={PM25_THRESHOLD}) ===")
print(f"Train: {len(train_df)}, Val: {len(val_df)}, Test: {len(test_df)}")
for name, d in [("Train", train_df), ("Val", val_df), ("Test", test_df)]:
    good = (d["label"] == 0).sum()
    bad = (d["label"] == 1).sum()
    print(f"  {name}: good={good} ({good/len(d)*100:.1f}%), bad={bad} ({bad/len(d)*100:.1f}%)")

# ── Normalize meteo ──────────────────────────────────────────────────────
meteo_scaler = StandardScaler()
meteo_scaler.fit(train_df[METEO_COLS].values)
for d in [train_df, val_df, test_df]:
    scaled = meteo_scaler.transform(d[METEO_COLS].values)
    d[["temp_scaled", "humidity_scaled", "pressure_scaled"]] = scaled

with open(SAVE_DIR / "meteo_scaler.pkl", "wb") as f:
    pickle.dump(meteo_scaler, f)

# ── Synthetic haze augmentation ─────────────────────────────────────────
class AddRandomHaze:
    """Synthetic haze/fog overlay — simulates bad air quality visually.
    Applies a semi-transparent gray/white layer with random density.
    More aggressive on images labeled 'bad' to reinforce the visual pattern."""
    def __init__(self, prob=0.3, max_intensity=0.5):
        self.prob = prob
        self.max_intensity = max_intensity

    def __call__(self, img):
        if np.random.random() < self.prob:
            # Random haze intensity
            intensity = np.random.uniform(0.15, self.max_intensity)
            # Gray-white haze color (slightly random tint)
            r = int(180 + np.random.uniform(-20, 20))
            g = int(180 + np.random.uniform(-20, 20))
            b = int(185 + np.random.uniform(-20, 20))
            haze_color = (r, g, b)

            # Create haze overlay
            haze = Image.new("RGB", img.size, haze_color)
            # Blur the haze for a fog-like effect
            blur_radius = np.random.uniform(0, 3)
            if blur_radius > 0:
                haze = haze.filter(ImageFilter.GaussianBlur(blur_radius))

            # Blend
            img = Image.blend(img, haze, intensity)
        return img


# ── CLIP transforms ─────────────────────────────────────────────────────
# CLIP uses its own normalization
CLIP_MEAN = [0.48145466, 0.4578275, 0.40821073]
CLIP_STD = [0.26862954, 0.26130258, 0.27577711]

train_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE + 30, IMG_SIZE + 30)),
    transforms.RandomCrop(IMG_SIZE),
    transforms.RandomHorizontalFlip(),
    transforms.RandomRotation(10),
    transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.05),
    AddRandomHaze(prob=HAZE_PROB, max_intensity=0.5),
    transforms.ToTensor(),
    transforms.Normalize(mean=CLIP_MEAN, std=CLIP_STD),
    transforms.RandomErasing(p=0.15, scale=(0.02, 0.15)),
])

eval_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=CLIP_MEAN, std=CLIP_STD),
])

# ── Dataset ─────────────────────────────────────────────────────────────
class SageAirDataset(Dataset):
    def __init__(self, dataframe, transform):
        self.df = dataframe.reset_index(drop=True)
        self.transform = transform

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        img = Image.open(row["img_path"]).convert("RGB")
        img = self.transform(img)
        met = torch.tensor([
            row["temp_scaled"], row["humidity_scaled"], row["pressure_scaled"]
        ], dtype=torch.float32)
        label = torch.tensor(row["label"], dtype=torch.long)
        return img, met, label


train_ds = SageAirDataset(train_df, train_transform)
val_ds = SageAirDataset(val_df, eval_transform)
test_ds = SageAirDataset(test_df, eval_transform)

train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,
                          num_workers=4, pin_memory=True)
val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False,
                        num_workers=4, pin_memory=True)
test_loader = DataLoader(test_ds, batch_size=BATCH_SIZE, shuffle=False,
                         num_workers=4, pin_memory=True)

print(f"\nDataLoader sizes: train={len(train_loader)}, val={len(val_loader)}, test={len(test_loader)}")

# ── Model: CLIP + meteo fusion ──────────────────────────────────────────
class AirQualityModelCLIP(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        # CLIP ViT-B/32 visual encoder
        clip_model, _, _ = open_clip.create_model_and_transforms(
            'ViT-B-32', pretrained='openai'
        )
        self.clip_visual = clip_model.visual  # the image encoder part only
        clip_dim = 512  # CLIP ViT-B/32 output dim

        # Meteo encoder
        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # Fused head
        self.head = nn.Sequential(
            nn.Linear(clip_dim + 32, 256),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, img, met):
        feats = self.clip_visual(img)      # [B, 512]
        feats = feats.float()              # CLIP may output float16
        met_feats = self.meteo_encoder(met)  # [B, 32]
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)


model = AirQualityModelCLIP(meteo_dim=3, dropout=DROPOUT).to(device)

# Count params
total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"\nModel: CLIP ViT-B/32 + meteo MLP")
print(f"Parameters: {total_params:,} total, {trainable_params:,} trainable")

# ── Training ────────────────────────────────────────────────────────────
n_good = (train_df["label"] == 0).sum()
n_bad = (train_df["label"] == 1).sum()
pos_weight = n_good / n_bad
print(f"Class weight: {pos_weight:.3f}")

criterion = nn.CrossEntropyLoss(
    weight=torch.tensor([1.0, pos_weight], dtype=torch.float32).to(device),
    label_smoothing=LABEL_SMOOTHING,
)

# Two param groups: CLIP visual (low LR) + head (higher LR)
clip_params = [p for p in model.clip_visual.parameters() if p.requires_grad]
clip_ids = {id(p) for p in clip_params}
head_params = [p for p in model.parameters() if p.requires_grad and id(p) not in clip_ids]

optimizer = optim.AdamW([
    {"params": clip_params, "lr": CLIP_LR},
    {"params": head_params, "lr": HEAD_LR},
], weight_decay=WEIGHT_DECAY)

scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max',
                                                  patience=3, factor=0.5)

best_val_f1 = 0.0
best_epoch = 0
patience_counter = 0
history = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": [],
           "val_f1": [], "val_auc": []}

print("\n=== Training v4 (CLIP + haze aug + threshold=35) ===")
for epoch in range(NUM_EPOCHS):
    t0 = time.time()

    # Train
    model.train()
    train_loss = 0.0
    train_correct = 0
    train_total = 0
    for imgs, mets, labels in train_loader:
        imgs, mets, labels = imgs.to(device), mets.to(device), labels.to(device)
        optimizer.zero_grad()
        outputs = model(imgs, mets)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        train_loss += loss.item() * imgs.size(0)
        preds = outputs.argmax(dim=1)
        train_correct += (preds == labels).sum().item()
        train_total += imgs.size(0)
    train_loss /= train_total
    train_acc = train_correct / train_total

    # Val
    model.eval()
    val_loss = 0.0
    val_correct = 0
    val_total = 0
    all_val_preds = []
    all_val_probs = []
    all_val_labels = []
    with torch.no_grad():
        for imgs, mets, labels in val_loader:
            imgs, mets, labels = imgs.to(device), mets.to(device), labels.to(device)
            outputs = model(imgs, mets)
            loss = criterion(outputs, labels)
            val_loss += loss.item() * imgs.size(0)
            preds = outputs.argmax(dim=1)
            probs = torch.softmax(outputs, dim=1)[:, 1]
            val_correct += (preds == labels).sum().item()
            val_total += imgs.size(0)
            all_val_preds.extend(preds.cpu().numpy())
            all_val_probs.extend(probs.cpu().numpy())
            all_val_labels.extend(labels.cpu().numpy())
    val_loss /= val_total
    val_acc = val_correct / val_total
    val_f1 = f1_score(all_val_labels, all_val_preds, average='binary')
    try:
        val_auc = roc_auc_score(all_val_labels, all_val_probs)
    except:
        val_auc = 0.0

    history["train_loss"].append(train_loss)
    history["val_loss"].append(val_loss)
    history["train_acc"].append(train_acc)
    history["val_acc"].append(val_acc)
    history["val_f1"].append(val_f1)
    history["val_auc"].append(val_auc)

    elapsed = time.time() - t0
    lr_clip = optimizer.param_groups[0]['lr']
    lr_head = optimizer.param_groups[1]['lr']
    print(f"Epoch {epoch+1:>2}/{NUM_EPOCHS} | {elapsed:.1f}s | LR clip={lr_clip:.1e} head={lr_head:.1e} | "
          f"Train loss={train_loss:.4f} acc={train_acc:.4f} | "
          f"Val loss={val_loss:.4f} acc={val_acc:.4f} f1={val_f1:.4f} auc={val_auc:.4f}")

    scheduler.step(val_f1)

    if val_f1 > best_val_f1:
        best_val_f1 = val_f1
        best_epoch = epoch + 1
        patience_counter = 0
        torch.save({
            "epoch": epoch + 1,
            "model_state_dict": model.state_dict(),
            "val_f1": val_f1,
            "val_acc": val_acc,
            "val_auc": val_auc,
        }, SAVE_DIR / "best_model.pt")
        print(f"  >> New best val F1={val_f1:.4f} (saved)")
    else:
        patience_counter += 1
        if patience_counter >= EARLY_STOP_PATIENCE:
            print(f"  Early stopping at epoch {epoch+1}")
            break

print(f"\nBest epoch: {best_epoch} (val F1={best_val_f1:.4f})")

# ── Test evaluation ─────────────────────────────────────────────────────
print("\n=== Test Evaluation ===")
checkpoint = torch.load(SAVE_DIR / "best_model.pt", weights_only=True)
model.load_state_dict(checkpoint["model_state_dict"])
model.eval()

all_test_preds = []
all_test_probs = []
all_test_labels = []
with torch.no_grad():
    for imgs, mets, labels in test_loader:
        imgs, mets, labels = imgs.to(device), mets.to(device), labels.to(device)
        outputs = model(imgs, mets)
        preds = outputs.argmax(dim=1)
        probs = torch.softmax(outputs, dim=1)[:, 1]
        all_test_preds.extend(preds.cpu().numpy())
        all_test_probs.extend(probs.cpu().numpy())
        all_test_labels.extend(labels.cpu().numpy())

test_acc = accuracy_score(all_test_labels, all_test_preds)
test_f1 = f1_score(all_test_labels, all_test_preds, average='binary')
try:
    test_auc = roc_auc_score(all_test_labels, all_test_probs)
except:
    test_auc = 0.0

print(f"Test Accuracy: {test_acc:.4f}")
print(f"Test F1:       {test_f1:.4f}")
print(f"Test AUC:      {test_auc:.4f}")
print()
print("Classification Report:")
print(classification_report(all_test_labels, all_test_preds,
                            target_names=["Good (0)", "Bad (1)"]))
print("Confusion Matrix:")
cm = confusion_matrix(all_test_labels, all_test_preds)
print(f"  True\\Pred  Good  Bad")
print(f"  Good       {cm[0][0]:>4}  {cm[0][1]:>4}")
print(f"  Bad        {cm[1][0]:>4}  {cm[1][1]:>4}")

# ── Compare with v1, v2 ─────────────────────────────────────────────────
v1_path = Path("/home/veday28/SageAir/models/v1_frozen/training_results.json")
v2_path = Path("/home/veday28/SageAir/models/v2_finetuned/training_results.json")
v3_path = Path("/home/veday28/SageAir/models/v3_clip_haze/training_results.json")
print(f"\n=== v1 vs v2 vs v3 vs v4 Comparison ===")
print(f"  {'Metric':<15} {'v1':<12} {'v2':<12} {'v3':<12} {'v4':<12}")
if v1_path.exists():
    with open(v1_path) as f:
        v1 = json.load(f)
    print(f"  {'Threshold':<15} {'35':<12} {'35':<12} {'15':<12} {str(PM25_THRESHOLD):<12}")
    print(f"  {'Backbone':<15} {'ResNet50':<12} {'ResNet50':<12} {'CLIP-ViT':<12} {'CLIP-ViT':<12}")
if v2_path.exists():
    with open(v2_path) as f:
        v2 = json.load(f)
if v3_path.exists():
    with open(v3_path) as f:
        v3 = json.load(f)
    print(f"  {'Test Acc':<15} {v1['test_acc']:<12.4f} {v2['test_acc']:<12.4f} {v3['test_acc']:<12.4f} {test_acc:<12.4f}")
    print(f"  {'Test F1':<15} {v1['test_f1']:<12.4f} {v2['test_f1']:<12.4f} {v3['test_f1']:<12.4f} {test_f1:<12.4f}")
    print(f"  {'Test AUC':<15} {v1['test_auc']:<12.4f} {v2['test_auc']:<12.4f} {v3['test_auc']:<12.4f} {test_auc:<12.4f}")

# ── Smoke test: run the external smoke image ────────────────────────────
SMOKE_IMG = "/home/veday28/SageAir/plugin/20260717_2000.01.jpg"
if Path(SMOKE_IMG).exists():
    print(f"\n=== Smoke Image Test ===")
    img = Image.open(SMOKE_IMG).convert("RGB")
    img_tensor = eval_transform(img).unsqueeze(0).to(device)
    # Use median meteo values as placeholders
    met_raw = np.array([[df["temperature"].median(), df["humidity"].median(), df["pressure"].median()]])
    met_scaled = meteo_scaler.transform(met_raw)
    met_tensor = torch.tensor(met_scaled, dtype=torch.float32).to(device)

    with torch.no_grad():
        outputs = model(img_tensor, met_tensor)
        probs = torch.softmax(outputs, dim=1)
        pred = probs.argmax(dim=1).item()
        print(f"  Smoke image prediction: {'GOOD' if pred == 0 else 'BAD'}")
        print(f"  P(good)={probs[0][0].item():.4f}, P(bad)={probs[0][1].item():.4f}")
        print(f"  Confidence={max(probs[0][0].item(), probs[0][1].item())*100:.1f}%")

# ── Save results ───────────────────────────────────────────────────────
results = {
    "version": "v4_clip_haze_th35",
    "best_epoch": best_epoch,
    "best_val_f1": best_val_f1,
    "test_acc": test_acc,
    "test_f1": test_f1,
    "test_auc": test_auc,
    "pm25_threshold": PM25_THRESHOLD,
    "backbone": "CLIP ViT-B/32",
    "test_nodes": TEST_NODES,
    "train_nodes": TRAIN_NODES,
    "clip_lr": CLIP_LR,
    "head_lr": HEAD_LR,
    "label_smoothing": LABEL_SMOOTHING,
    "haze_prob": HAZE_PROB,
    "history": history,
}
with open(SAVE_DIR / "training_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"\nResults saved to {SAVE_DIR / 'training_results.json'}")
print(f"Best model saved to {SAVE_DIR / 'best_model.pt'}")
print(f"Scaler saved to {SAVE_DIR / 'meteo_scaler.pkl'}")
print("\nDone.")
