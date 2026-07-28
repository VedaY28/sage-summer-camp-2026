#!/usr/bin/env python3
"""
SageAir v2 — fine-tuned ResNet50 + stronger augmentation.

Changes from v1 (train.py):
  1. Unfreeze ResNet50 layer4 (last residual block) for fine-tuning
  2. Stronger augmentation: RandomErasing, GaussianBlur, stronger ColorJitter
  3. Lower LR for fine-tuned backbone (1e-4) vs head LR (1e-3) via param groups
  4. Label smoothing (0.1) for noisy PM2.5 labels
  5. Saves to models/v2_finetuned/ (v1 preserved in models/v1_frozen/)
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
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, models
from PIL import Image
from sklearn.metrics import (
    classification_report, confusion_matrix,
    roc_auc_score, f1_score, accuracy_score
)
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

# ── Config ──────────────────────────────────────────────────────────────
CSV_PATH = "/home/veday28/SageAir/sageair_2week_image_data.csv"
IMG_DIR = Path("/home/veday28/SageAir/images")
SAVE_DIR = Path("/home/veday28/SageAir/models/v2_finetuned")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

PM25_THRESHOLD = 35.0
IMG_SIZE = 224
BATCH_SIZE = 64
NUM_EPOCHS = 30
# Two LR groups: lower for fine-tuned backbone, higher for head
BACKBONE_LR = 1e-4
HEAD_LR = 1e-3
WEIGHT_DECAY = 1e-4
DROPOUT = 0.3
LABEL_SMOOTHING = 0.1
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
    print(f"GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
print()

# ── Load and prepare data ───────────────────────────────────────────────
df = pd.read_csv(CSV_PATH)
print(f"Total rows: {len(df)}")

# Derive label
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
    print("ERROR: Some images not found.")
    sys.exit(1)

# Handle NaN in meteo columns
for col in METEO_COLS:
    n_null = df[col].isna().sum()
    if n_null > 0:
        print(f"Filling {n_null} NaN in {col} with median")
        df[col] = df[col].fillna(df[col].median())

# ── Split ───────────────────────────────────────────────────────────────
test_df = df[df["node"].isin(TEST_NODES)].copy()
trainval_df = df[df["node"].isin(TRAIN_NODES)].copy()

# Stratified split for val
train_df, val_df = train_test_split(
    trainval_df, test_size=0.2, stratify=trainval_df["label"], random_state=SEED
)

print(f"\n=== Split ===")
print(f"Train: {len(train_df)} rows (nodes: {sorted(train_df['node'].unique())})")
print(f"Val:   {len(val_df)} rows")
print(f"Test:  {len(test_df)} rows (nodes: {sorted(test_df['node'].unique())})")
for name, d in [("Train", train_df), ("Val", val_df), ("Test", test_df)]:
    good = (d["label"] == 0).sum()
    bad = (d["label"] == 1).sum()
    print(f"  {name}: good={good} ({good/len(d)*100:.1f}%), bad={bad} ({bad/len(d)*100:.1f}%)")

# ── Normalize meteorology ──────────────────────────────────────────────
meteo_scaler = StandardScaler()
meteo_scaler.fit(train_df[METEO_COLS].values)
for d in [train_df, val_df, test_df]:
    scaled = meteo_scaler.transform(d[METEO_COLS].values)
    d[["temp_scaled", "humidity_scaled", "pressure_scaled"]] = scaled

with open(SAVE_DIR / "meteo_scaler.pkl", "wb") as f:
    pickle.dump(meteo_scaler, f)
print(f"\nSaved meteo scaler to {SAVE_DIR / 'meteo_scaler.pkl'}")

# ── Class weights ───────────────────────────────────────────────────────
n_good = (train_df["label"] == 0).sum()
n_bad = (train_df["label"] == 1).sum()
pos_weight = n_good / n_bad
print(f"Class weight (bad/good ratio): {pos_weight:.3f}")

# ── STRONGER augmentation (v2 key change) ───────────────────────────────
train_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE + 30, IMG_SIZE + 30)),
    transforms.RandomCrop(IMG_SIZE),
    transforms.RandomHorizontalFlip(),
    transforms.RandomRotation(10),
    transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.05),
    transforms.RandomApply([transforms.GaussianBlur(kernel_size=3)], p=0.3),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    transforms.RandomErasing(p=0.2, scale=(0.02, 0.15)),
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

# ── Model (with fine-tunable layer4) ───────────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        resnet = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)
        # Split: frozen stem (conv1..layer3) + fine-tunable layer4
        self.image_stem = nn.Sequential(
            resnet.conv1, resnet.bn1, resnet.relu, resnet.maxpool,
            resnet.layer1, resnet.layer2, resnet.layer3,
        )
        self.image_layer4 = resnet.layer4  # fine-tunable
        self.global_pool = nn.AdaptiveAvgPool2d(1)  # squash to [B, 2048, 1, 1]
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
        feats = self.image_stem(img)
        feats = self.image_layer4(feats)
        feats = self.global_pool(feats)    # [B, 2048, 1, 1]
        feats = feats.flatten(1)           # [B, 2048]
        feats = self.img_proj(feats)   # [B, 512]
        met_feats = self.meteo_encoder(met)
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)


model = AirQualityModel(meteo_dim=3, dropout=DROPOUT).to(device)

# Freeze stem, unfreeze layer4
for param in model.image_stem.parameters():
    param.requires_grad = False
for param in model.image_layer4.parameters():
    param.requires_grad = True  # fine-tune these

total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"\nModel parameters: {total_params:,} total, {trainable_params:,} trainable")
print(f"  (layer4 ~15M params is now fine-tunable)")

# ── Training ───────────────────────────────────────────────────────────
# Two param groups: lower LR for backbone, higher LR for head
criterion = nn.CrossEntropyLoss(
    weight=torch.tensor([1.0, pos_weight], dtype=torch.float32).to(device),
    label_smoothing=LABEL_SMOOTHING,
)

backbone_params = list(p for p in model.image_layer4.parameters() if p.requires_grad)
backbone_param_ids = {id(p) for p in backbone_params}
head_params = [p for p in model.parameters() if p.requires_grad and id(p) not in backbone_param_ids]

optimizer = optim.Adam([
    {"params": backbone_params, "lr": BACKBONE_LR},
    {"params": head_params, "lr": HEAD_LR},
], weight_decay=WEIGHT_DECAY)

scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max',
                                                  patience=3, factor=0.5)

best_val_f1 = 0.0
best_epoch = 0
patience_counter = 0
history = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": [],
           "val_f1": [], "val_auc": []}

print("\n=== Training (v2: fine-tuned layer4 + stronger augmentation) ===")
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
    lr_bb = optimizer.param_groups[0]['lr']
    lr_hd = optimizer.param_groups[1]['lr']
    print(f"Epoch {epoch+1:>2}/{NUM_EPOCHS} | {elapsed:.1f}s | LR bb={lr_bb:.1e} hd={lr_hd:.1e} | "
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

# ── Compare with v1 ────────────────────────────────────────────────────
v1_results_path = Path("/home/veday28/SageAir/models/v1_frozen/training_results.json")
if v1_results_path.exists():
    with open(v1_results_path) as f:
        v1 = json.load(f)
    print(f"\n=== v1 vs v2 Comparison ===")
    print(f"  {'Metric':<15} {'v1 (frozen)':<15} {'v2 (finetuned)':<15}")
    print(f"  {'Test Acc':<15} {v1['test_acc']:<15.4f} {test_acc:<15.4f}")
    print(f"  {'Test F1':<15} {v1['test_f1']:<15.4f} {test_f1:<15.4f}")
    print(f"  {'Test AUC':<15} {v1['test_auc']:<15.4f} {test_auc:<15.4f}")

# ── Save results ───────────────────────────────────────────────────────
results = {
    "version": "v2_finetuned",
    "best_epoch": best_epoch,
    "best_val_f1": best_val_f1,
    "test_acc": test_acc,
    "test_f1": test_f1,
    "test_auc": test_auc,
    "pm25_threshold": PM25_THRESHOLD,
    "test_nodes": TEST_NODES,
    "train_nodes": TRAIN_NODES,
    "backbone_lr": BACKBONE_LR,
    "head_lr": HEAD_LR,
    "label_smoothing": LABEL_SMOOTHING,
    "history": history,
}
with open(SAVE_DIR / "training_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"\nResults saved to {SAVE_DIR / 'training_results.json'}")
print(f"Best model saved to {SAVE_DIR / 'best_model.pt'}")
print(f"Scaler saved to {SAVE_DIR / 'meteo_scaler.pkl'}")
print("\nDone.")
