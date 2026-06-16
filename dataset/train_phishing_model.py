"""Offline trainer for TaigaBot's phishing/scam detector.

Downloads the `wangyuancheng/discord-phishing-scam-clean` dataset (1,830 labelled
Discord messages), trains a small Naive-Bayes log-odds classifier, tunes a
decision threshold that favours PRECISION (we'd rather miss a scam than delete a
real member's message), and writes the learned weights to
`dataset/phishing_model.json`.

This is a BUILD-TIME tool — it is never imported by the running bot. It uses only
the Python standard library (urllib + csv), so there are no third-party
dependencies to install; just run it whenever you want to refresh the model:

    python dataset/train_phishing_model.py

The shared `tokenize()` from `utils/phishing.py` is reused here so the features
the model learns are exactly the ones it sees at runtime.
"""
from __future__ import annotations

import csv
import io
import json
import math
import pathlib
import random
import sys
import time
import urllib.request

# Make the repo root importable so we share the runtime tokenizer.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from utils.phishing import tokenize  # noqa: E402

DATA_URL = (
    "https://huggingface.co/datasets/wangyuancheng/discord-phishing-scam-clean/"
    "resolve/main/discord-phishing-scam-detection.csv"
)
CACHE_CSV = REPO_ROOT / "dataset" / "discord-phishing-scam-detection.csv"
OUT_PATH = REPO_ROOT / "dataset" / "phishing_model.json"

ALPHA = 1.0            # Laplace smoothing
MIN_DOC_FREQ = 2       # ignore tokens seen in fewer than this many messages
TARGET_PRECISION = 0.92  # tune the threshold to hit at least this precision
TEST_FRACTION = 0.2
SEED = 1337


def load_rows() -> list[tuple[int, str]]:
    """Return (label, msg_content) pairs, downloading + caching the CSV once."""
    if not CACHE_CSV.exists():
        print(f"Downloading dataset -> {CACHE_CSV.name} ...")
        CACHE_CSV.parent.mkdir(exist_ok=True)
        with urllib.request.urlopen(DATA_URL, timeout=60) as resp:
            CACHE_CSV.write_bytes(resp.read())
    raw = CACHE_CSV.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    rows = [(int(r["label"]), r["msg_content"] or "") for r in reader if r.get("label")]
    print(f"Loaded {len(rows)} rows.")
    return rows


def train(samples: list[tuple[int, list[str]]]) -> tuple[dict[str, float], float]:
    """Train Naive-Bayes log-odds weights from (label, tokens) pairs.

    weight[token] = log P(token | scam) − log P(token | ham)  (Laplace-smoothed)
    bias          = log P(scam) − log P(ham)   (the class prior, in log-odds)
    """
    count = {0: {}, 1: {}}     # per-class token counts
    total = {0: 0, 1: 0}       # per-class total token count
    doc_freq: dict[str, int] = {}
    n = {0: 0, 1: 0}
    for label, tokens in samples:
        n[label] += 1
        for tok in set(tokens):
            doc_freq[tok] = doc_freq.get(tok, 0) + 1
        for tok in tokens:
            count[label][tok] = count[label].get(tok, 0) + 1
            total[label] += 1

    vocab = {t for t, df in doc_freq.items() if df >= MIN_DOC_FREQ}
    v = len(vocab)
    denom0 = total[0] + ALPHA * v
    denom1 = total[1] + ALPHA * v
    weights = {}
    for tok in vocab:
        p1 = (count[1].get(tok, 0) + ALPHA) / denom1
        p0 = (count[0].get(tok, 0) + ALPHA) / denom0
        weights[tok] = math.log(p1) - math.log(p0)
    bias = math.log(n[1]) - math.log(n[0])
    return weights, bias


def score(weights: dict[str, float], bias: float, tokens: list[str]) -> float:
    return bias + sum(weights[t] for t in tokens if t in weights)


def stratified_split(samples, frac, seed):
    rng = random.Random(seed)
    by_label = {0: [], 1: []}
    for s in samples:
        by_label[s[0]].append(s)
    train_set, test_set = [], []
    for label, items in by_label.items():
        rng.shuffle(items)
        cut = int(len(items) * (1 - frac))
        train_set += items[:cut]
        test_set += items[cut:]
    rng.shuffle(train_set)
    rng.shuffle(test_set)
    return train_set, test_set


def pick_threshold(scored: list[tuple[float, int]]):
    """Choose the threshold maximising F1 subject to precision ≥ TARGET_PRECISION.

    `scored` is a list of (score, true_label). Falls back to the most-precise
    threshold if none reaches the precision target.
    """
    candidates = sorted({s for s, _ in scored})
    best = None          # (f1, precision, recall, threshold)
    best_precise = None  # fallback: highest precision seen
    for thr in candidates:
        tp = sum(1 for s, y in scored if s > thr and y == 1)
        fp = sum(1 for s, y in scored if s > thr and y == 0)
        fn = sum(1 for s, y in scored if s <= thr and y == 1)
        if tp == 0:
            continue
        precision = tp / (tp + fp)
        recall = tp / (tp + fn)
        f1 = 2 * precision * recall / (precision + recall)
        if best_precise is None or precision > best_precise[1]:
            best_precise = (f1, precision, recall, thr)
        if precision >= TARGET_PRECISION and (best is None or f1 > best[0]):
            best = (f1, precision, recall, thr)
    return best or best_precise


def main() -> None:
    rows = load_rows()
    samples = [(label, tokenize(text)) for label, text in rows]

    # Hold out a stratified test set to choose the threshold + report honest
    # metrics, then retrain on ALL data for the shipped weights.
    train_set, test_set = stratified_split(samples, TEST_FRACTION, SEED)
    w, b = train(train_set)
    scored = [(score(w, b, toks), y) for y, toks in test_set]
    f1, precision, recall, threshold = pick_threshold(scored)
    print(
        f"Held-out metrics @ threshold={threshold:.3f}: "
        f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f} "
        f"(test n={len(test_set)})"
    )

    # Final model: retrain on the full dataset, keep the tuned threshold.
    weights, bias = train(samples)
    model = {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "wangyuancheng/discord-phishing-scam-clean",
        "algorithm": "multinomial-naive-bayes-logodds",
        "alpha": ALPHA,
        "min_doc_freq": MIN_DOC_FREQ,
        "bias": bias,
        "threshold": threshold,
        "vocab_size": len(weights),
        "metrics": {"precision": precision, "recall": recall, "f1": f1},
        "weights": weights,
    }
    OUT_PATH.write_text(json.dumps(model), encoding="utf-8")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({len(weights)} tokens, {size_kb:.1f} KB).")


if __name__ == "__main__":
    main()
