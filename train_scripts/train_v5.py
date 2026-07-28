#!/usr/bin/env python3
"""
SageAir v5 — Multimodal CLIP + meteorology classifier.

Trains on the images_v2/ split (train/val/test/good/bad) using:
  - CLIP ViT-B/32 image encoder (fine-tuned)
  - Meteorology encoder (temp, humidity, pressure -> MLP)
  - Synthetic haze augmentation on training images
  - Label from purple_air_pm25 >= 151 (bad) vs < 151 (good)

Meteo data is matched from all_data_with_weathervar.csv by filename.
"""
import os
import sys
import json
import time
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
from PIL import Image
from sklearn.metrics import (
    classification_report, confusion_matrix,
    roc_auc_score, f1_score, accuracy_score
)
from sklearn.preprocessing import StandardScaler

# ── Config ──────────────────────────────────────────────────────────────
CSV_PATH = "/home/veday28/SageAir/all_data_with_weathervar.csv"
IMG_ROOT = Path("/home/veday28/SageAir/images_v2")
SAVE_DIR = Path("/home/veday28/SageAir/models/v5_clip_haze_pa151")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

PM25_THRESHOLD = 151.0
IMG_SIZE = 224
BATCH_SIZE = 32
NUM_EPOCHS = 30
HEAD_LR = 5e-4
BACKBONE_LR = 1e-5   # CLIP visual encoder fine-tune (low LR)
WEIGHT_DECAY = 1e-4
DROPOUT = 0.3
LABEL_SMOOTHING = 0.1
EARLY_STOP_PATIENCE = 7
SEED = 42

METEO_COLS = ["temp", "humidity", "pressure"]

# CLIP normalization
IMG_MEAN = [0.48145466, 0.4578275, 0.40821073]
IMG_STD = [0.26862954, 0.26130258, 0.27577711]

# ── Synthetic haze augmentation ─────────────────────────────────────────
class AddRandomHaze:
    """Synthetic haze/fog overlay — simulates bad air quality visually."""
    def __init__(self, prob=0.3, max_intensity=0.5):
        self.prob = prob
        self.max_intensity = max_intensity

    def __call__(self, img):
        if np.random.random() < self.prob:
            intensity = np.random.uniform(0.1, self.max_intensity)
            haze_color = np.random.uniform(0.6, 0.9, size=3).astype(np.float32)
            arr = np.array(img, dtype=np.float32)
            arr = arr * (1 - intensity) + haze_color * intensity * 255
            arr = np.clip(arr, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)
        return img


# ── Reproducibility ────────────────────────────────────────────────────
torch.manual_seed(SEED)
np.random.seed(SEED)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device: {device}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
print()

# ── Load CSV and build filename -> meteo lookup ────────────────────────
df = pd.read_csv(CSV_PATH)
df["filename"] = df["filename"].str.strip()
meteo_lookup = df.set_index("filename")[METEO_COLS].to_dict("index")

# Verify all split images have meteo data
for split in ["train", "val", "test"]:
    for cls in ["good", "bad"]:
        folder = IMG_ROOT / split / cls
        files = [f for f in os.listdir(folder) if f.endswith(".jpg")]
        missing = [f for f in files if f not in meteo_lookup]
        if missing:
            print(f"WARNING: {split}/{cls} has {len(missing)} images without meteo data")

print(f"Total rows in CSV: {len(df)}")
print()

# ── Collect all images per split and fit scaler on train ───────────────
def collect_split(split_name):
    """Collect (image_path, label, filename) for a split."""
    items = []
    for cls_name, label in [("good", 0), ("bad", 1)]:
        folder = IMG_ROOT / split_name / cls_name
        files = sorted([f for f in os.listdir(folder) if f.endswith(".jpg")])
        for fname in files:
            items.append((folder / fname, label, fname))
    return items

train_items = collect_split("train")
val_items = collect_split("val")
test_items = collect_split("test")

print(f"=== Split sizes ===")
print(f"Train: {len(train_items)} (good={sum(1 for _,l,_ in train_items if l==0)}, bad={sum(1 for _,l,_ in train_items if l==1)})")
print(f"Val:   {len(val_items)} (good={sum(1 for _,l,_ in val_items if l==0)}, bad={sum(1 for _,l,_ in val_items if l==1)})")
print(f"Test:  {len(test_items)} (good={sum(1 for _,l,_ in test_items if l==0)}, bad={sum(1 for _,l,_ in test_items if l==1)})")

# Fit scaler on train meteo values
train_meteo = []
for _, _, fname in train_items:
    row = meteo_lookup[fname]
    train_meteo.append([row["temp"], row["humidity"], row["pressure"]])

meteo_scaler = StandardScaler()
meteo_scaler.fit(train_meteo)

with open(SAVE_DIR / "meteo_scaler.pkl", "wb") as f:
    pickle.dump(meteo_scaler, f)
print(f"\nSaved meteo scaler to {SAVE_DIR / 'meteo_scaler.pkl'}")
print(f"Scaler means: {meteo_scaler.mean_}")
print(f"Scaler stds:  {meteo_scaler.scale_}")

# ── Transforms ─────────────────────────────────────────────────────────
train_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE + 30, IMG_SIZE + 30)),
    transforms.RandomCrop(IMG_SIZE),
    transforms.RandomHorizontalFlip(),
    transforms.RandomRotation(10),
    transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.05),
    transforms.RandomApply([transforms.GaussianBlur(kernel_size=3)], p=0.3),
    AddRandomHaze(prob=0.3, max_intensity=0.5),
    transforms.ToTensor(),
    transforms.Normalize(mean=IMG_MEAN, std=IMG_STD),
    transforms.RandomErasing(p=0.2, scale=(0.02, 0.15)),
])

eval_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=IMG_MEAN, std=IMG_STD),
])

# ── Dataset ─────────────────────────────────────────────────────────────
class SageAirDataset(Dataset):
    def __init__(self, items, meteo_lookup, scaler, transform):
        self.items = items
        self.meteo_lookup = meteo_lookup
        self.scaler = scaler
        self.transform = transform

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        img_path, label, fname = self.items[idx]
        img = Image.open(img_path).convert("RGB")
        img = self.transform(img)
        # Get meteo and scale
        row = self.meteo_lookup[fname]
        meteo_raw = np.array([[row["temp"], row["humidity"], row["pressure"]]])
        meteo_scaled = self.scaler.transform(meteo_raw)[0]
        met = torch.tensor(meteo_scaled, dtype=torch.float32)
        label = torch.tensor(label, dtype=torch.long)
        return img, met, label

train_ds = SageAirDataset(train_items, meteo_lookup, meteo_scaler, train_transform)
val_ds = SageAirDataset(val_items, meteo_lookup, meteo_scaler, eval_transform)
test_ds = SageAirDataset(test_items, meteo_lookup, meteo_scaler, eval_transform)

train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,
                          num_workers=4, pin_memory=True)
val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False,
                        num_workers=4, pin_memory=True)
test_loader = DataLoader(test_ds, batch_size=BATCH_SIZE, shuffle=False,
                         num_workers=4, pin_memory=True)

print(f"\nDataLoader batches: train={len(train_loader)}, val={len(val_loader)}, test={len(test_loader)}")

# ── Model ───────────────────────────────────────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        import open_clip
        clip_model, _, _ = open_clip.create_model_and_transforms(
            'ViT-B-32', pretrained='openai'
        )
        self.clip_visual = clip_model.visual
        clip_dim = 512

        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
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
        feats = self.clip_visual(img)
        feats = feats.float()
        met_feats = self.meteo_encoder(met)
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)

model = AirQualityModel(meteo_dim=3, dropout=DROPOUT).to(device)

# Two param groups: CLIP backbone (low LR) + head (higher LR)
# Use id() for identity checks — `p not in list_of_tensors` does elementwise
# comparison and raises "Boolean value of Tensor with more than one value is ambiguous"
backbone_params = [p for p in model.clip_visual.parameters() if p.requires_grad]
backbone_ids = {id(p) for p in backbone_params}
head_params = [p for p in model.parameters()
               if p.requires_grad and id(p) not in backbone_ids]

total_params = sum(p.numel() for p in model.parameters())
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"\nModel: {total_params:,} total, {trainable_params:,} trainable")

# ── Loss + optimizer ─────────────────────────────────────────────────────
# Classes are balanced (equal good/bad per split), so no class weights needed
criterion = nn.CrossEntropyLoss(label_smoothing=LABEL_SMOOTHING)

optimizer = optim.Adam([
    {"params": backbone_params, "lr": BACKBONE_LR},
    {"params": head_params, "lr": HEAD_LR},
], weight_decay=WEIGHT_DECAY)

scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max',
                                                  patience=3, factor=0.5)

# ── Training loop ───────────────────────────────────────────────────────
best_val_f1 = 0.0
best_epoch = 0
patience_counter = 0
history = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": [],
           "val_f1": [], "val_auc": []}

print(f"\n=== Training (v5: CLIP + haze aug + meteo, PA threshold 151) ===")
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

# ── Save results ────────────────────────────────────────────────────────
results = {
    "version": "v5_clip_haze_pa151",
    "best_epoch": best_epoch,
    "best_val_f1": best_val_f1,
    "test_acc": test_acc,
    "test_f1": test_f1,
    "test_auc": test_auc,
    "pm25_threshold": PM25_THRESHOLD,
    "pm25_source": "purple_air_pm25",
    "backbone_lr": BACKBONE_LR,
    "head_lr": HEAD_LR,
    "label_smoothing": LABEL_SMOOTHING,
    "haze_aug_prob": 0.3,
    "train_size": len(train_items),
    "val_size": len(val_items),
    "test_size": len(test_items),
    "split": "70/20/10 stratified by date, balanced good/bad",
    "history": history,
}
with open(SAVE_DIR / "training_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"\nResults saved to {SAVE_DIR / 'training_results.json'}")
print(f"Best model saved to {SAVE_DIR / 'best_model.pt'}")
print(f"Scaler saved to {SAVE_DIR / 'meteo_scaler.pkl'}")
print("\nDone.")
