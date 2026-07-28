#!/usr/bin/env python3
"""
SageAir multimodal air quality classifier.

Architecture:
  Image -> frozen ResNet50 (pretrained) -> 512-d embedding
  [temp, humidity, pressure] -> normalized 3-d vector
  [image_embedding ; met_vector] -> MLP head -> binary (good/bad)

Label: raw pm25 > 35 (EPA 24-hr threshold) -> bad (1), else good (0)

Split (cross-node):
  Test:  W0A4, W095 (1349 rows) — never seen during training
  Train: 80% of W0A0, W09E, W099 (2048 rows)
  Val:   20% of W0A0, W09E, W099 (512 rows)
"""
import os
import sys
import json
import hashlib
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, models
from PIL import Image
from sklearn.metrics import (
    classification_report, confusion_matrix,
    roc_auc_score, f1_score, accuracy_score
)
from sklearn.preprocessing import StandardScaler

# ── Config ──────────────────────────────────────────────────────────────
CSV_PATH = "/home/veday28/SageAir/sageair_2week_image_data.csv"
IMG_DIR = Path("/home/veday28/SageAir/images")
SAVE_DIR = Path("/home/veday28/SageAir/models")
SAVE_DIR.mkdir(exist_ok=True)

PM25_THRESHOLD = 35.0    # EPA 24-hr threshold (µg/m³)
IMG_SIZE = 224
BATCH_SIZE = 64
NUM_EPOCHS = 30
LR = 1e-3
WEIGHT_DECAY = 1e-4
DROPOUT = 0.3
EARLY_STOP_PATIENCE = 7
SEED = 42

TEST_NODES = ["W0A4", "W095"]
TRAIN_NODES = ["W0A0", "W09E", "W099"]
METEO_COLS = ["temperature", "humidity", "pressure"]

# ── Reproducibility ────────────────────────────────────────────────────
torch.manual_seed(SEED)
np.random.seed(SEED)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    gpu_mem = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"GPU memory: {gpu_mem:.1f} GB")
print()

# ── Load and prepare data ───────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
print(f"Total rows: {len(df)}")

# Derive label from raw pm25
df["label"] = (df["raw pm25"] > PM25_THRESHOLD).astype(int)

# Map image URL to cached filename
def url_to_path(url):
    fname = hashlib.sha1(str(url).encode()).hexdigest()[:16] + ".jpg"
    return IMG_DIR / fname

df["img_path"] = df["image url"].apply(url_to_path)

# Verify all images exist
missing = df[~df["img_path"].apply(lambda p: p.exists())]
print(f"Missing images: {len(missing)}")
if len(missing) > 0:
    print("WARNING: Some images not found. Check download.")
    sys.exit(1)

# Handle any NaN in meteo columns — fill with column median
for col in METEO_COLS:
    n_null = df[col].isna().sum()
    if n_null > 0:
        print(f"Filling {n_null} NaN in {col} with median")
        df[col] = df[col].fillna(df[col].median())

# ── Split ───────────────────────────────────────────────────────────────
test_df = df[df["node"].isin(TEST_NODES)].copy()
trainval_df = df[df["node"].isin(TRAIN_NODES)].copy()

# Stratified split for val (preserves both classes in val)
# Cross-node test set already handles generalization; val just needs both classes
from sklearn.model_selection import train_test_split
train_df, val_df = train_test_split(
    trainval_df, test_size=0.2, stratify=trainval_df["label"], random_state=SEED
)

print(f"\n=== Split ===")
print(f"Train: {len(train_df)} rows (nodes: {sorted(train_df['node'].unique())})")
print(f"Val:   {len(val_df)} rows")
print(f"Test:  {len(test_df)} rows (nodes: {sorted(test_df['node'].unique())})")

# Class distribution
for name, d in [("Train", train_df), ("Val", val_df), ("Test", test_df)]:
    good = (d["label"] == 0).sum()
    bad = (d["label"] == 1).sum()
    print(f"  {name}: good={good} ({good/len(d)*100:.1f}%), bad={bad} ({bad/len(d)*100:.1f}%)")

# ── Normalize meteorology (fit on train only) ──────────────────────────
meteo_scaler = StandardScaler()
meteo_scaler.fit(train_df[METEO_COLS].values)

for d in [train_df, val_df, test_df]:
    scaled = meteo_scaler.transform(d[METEO_COLS].values)
    d[["temp_scaled", "humidity_scaled", "pressure_scaled"]] = scaled

# Save scaler for inference
import pickle
with open(SAVE_DIR / "meteo_scaler.pkl", "wb") as f:
    pickle.dump(meteo_scaler, f)
print(f"\nSaved meteo scaler to {SAVE_DIR / 'meteo_scaler.pkl'}")

# ── Class weights for imbalanced training ───────────────────────────────
n_good = (train_df["label"] == 0).sum()
n_bad = (train_df["label"] == 1).sum()
pos_weight = torch.tensor([n_good / n_bad], dtype=torch.float32).to(device)
print(f"Class weight (bad/good ratio): {pos_weight.item():.3f}")

# ── Image transforms ────────────────────────────────────────────────────
train_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE + 20, IMG_SIZE + 20)),
    transforms.RandomCrop(IMG_SIZE),
    transforms.RandomHorizontalFlip(),
    transforms.ColorJitter(brightness=0.1, contrast=0.1, saturation=0.1),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

eval_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

# ── Dataset class ───────────────────────────────────────────────────────
class SageAirDataset(Dataset):
    def __init__(self, dataframe, transform):
        self.df = dataframe.reset_index(drop=True)
        self.transform = transform

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        # Load image
        img = Image.open(row["img_path"]).convert("RGB")
        img = self.transform(img)
        # Meteorology vector
        met = torch.tensor([
            row["temp_scaled"], row["humidity_scaled"], row["pressure_scaled"]
        ], dtype=torch.float32)
        # Label
        label = torch.tensor(row["label"], dtype=torch.long)
        return img, met, label


train_ds = SageAirDataset(train_df, train_transform)
val_ds = SageAirDataset(val_df, eval_transform)
test_ds = SageAirDataset(test_df, eval_transform)

train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,
                          num_workers=4, pin_memory=True, drop_last=False)
val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False,
                        num_workers=4, pin_memory=True)
test_loader = DataLoader(test_ds, batch_size=BATCH_SIZE, shuffle=False,
                         num_workers=4, pin_memory=True)

print(f"\nDataLoader sizes: train={len(train_loader)}, val={len(val_loader)}, test={len(test_loader)}")

# ── Model ──────────────────────────────────────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        # Image encoder: pretrained ResNet50, frozen
        resnet = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)
        # Remove the final FC layer, keep the 2048-d features
        self.image_encoder = nn.Sequential(*list(resnet.children())[:-1])  # -> [B, 2048, 1, 1]
        for param in self.image_encoder.parameters():
            param.requires_grad = False

        # Reduce 2048 -> 512
        self.img_proj = nn.Linear(2048, 512)

        # Meteo encoder
        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 16),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # Fused head
        self.head = nn.Sequential(
            nn.Linear(512 + 16, 256),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, img, met):
        # Image features
        feats = self.image_encoder(img)           # [B, 2048, 1, 1]
        feats = feats.flatten(1)                   # [B, 2048]
        feats = self.img_proj(feats)               # [B, 512]
        # Meteo features
        met_feats = self.meteo_encoder(met)        # [B, 16]
        # Fuse
        combined = torch.cat([feats, met_feats], dim=1)  # [B, 528]
        out = self.head(combined)                  # [B, 2]
        return out


model = AirQualityModel(meteo_dim=3, dropout=DROPOUT).to(device)

# Count parameters
total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"\nModel parameters: {total_params:,} total, {trainable_params:,} trainable")

# ── Training ───────────────────────────────────────────────────────────
# Weighted CE loss: upweight "bad" class to counter imbalance
criterion = nn.CrossEntropyLoss(
    weight=torch.tensor([1.0, pos_weight.item()], dtype=torch.float32).to(device)
)
optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                       lr=LR, weight_decay=WEIGHT_DECAY)
scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max',
                                                  patience=3, factor=0.5)

best_val_f1 = 0.0
best_epoch = 0
patience_counter = 0
history = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": [],
           "val_f1": [], "val_auc": []}

print("\n=== Training ===")
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

    # Track history
    history["train_loss"].append(train_loss)
    history["val_loss"].append(val_loss)
    history["train_acc"].append(train_acc)
    history["val_acc"].append(val_acc)
    history["val_f1"].append(val_f1)
    history["val_auc"].append(val_auc)

    elapsed = time.time() - t0
    lr_now = optimizer.param_groups[0]['lr']
    print(f"Epoch {epoch+1:>2}/{NUM_EPOCHS} | {elapsed:.1f}s | "
          f"LR={lr_now:.1e} | "
          f"Train loss={train_loss:.4f} acc={train_acc:.4f} | "
          f"Val loss={val_loss:.4f} acc={val_acc:.4f} f1={val_f1:.4f} auc={val_auc:.4f}")

    # LR scheduler
    scheduler.step(val_f1)

    # Early stopping
    if val_f1 > best_val_f1:
        best_val_f1 = val_f1
        best_epoch = epoch + 1
        patience_counter = 0
        # Save best model
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
            print(f"  Early stopping at epoch {epoch+1} (no improvement for {EARLY_STOP_PATIENCE} epochs)")
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

# ── Save results ───────────────────────────────────────────────────────
results = {
    "best_epoch": best_epoch,
    "best_val_f1": best_val_f1,
    "test_acc": test_acc,
    "test_f1": test_f1,
    "test_auc": test_auc,
    "pm25_threshold": PM25_THRESHOLD,
    "test_nodes": TEST_NODES,
    "train_nodes": TRAIN_NODES,
    "history": history,
}
with open(SAVE_DIR / "training_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"\nResults saved to {SAVE_DIR / 'training_results.json'}")
print(f"Best model saved to {SAVE_DIR / 'best_model.pt'}")
print(f"Scaler saved to {SAVE_DIR / 'meteo_scaler.pkl'}")
print("\nDone.")
