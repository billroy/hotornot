#!/usr/bin/env python3
"""Compare deployed Jev history with a local Jeff server."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


QUESTION = {
    "destination": {
        "type": "choice",
        "instructions": "Where should this one go?",
        "criteria": {"heaven": None, "hell": None, "purgatory": None},
    }
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--model", default="jeff-latest")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--plot", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def load_history(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["subject"]: row for row in map(json.loads, handle) if row}


def evaluate(server: str, model: str, subject: str) -> dict:
    body = json.dumps(
        {"model": model, "state": subject, "questions": QUESTION}
    ).encode()
    request = urllib.request.Request(
        f"{server.rstrip('/')}/v1/systemone",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            answer = payload["answers"]["destination"]
            return {
                "subject": subject,
                "model": payload.get("model"),
                "choice": answer["choice"],
                "confidence": answer["confidence"],
                "probabilities": answer["probabilities"],
            }
        except urllib.error.HTTPError as exc:
            if exc.code not in (503, 529) or attempt == 6:
                raise
            retry_after = float(exc.headers.get("Retry-After", "0.5"))
            time.sleep(max(retry_after, 0.25) * (attempt + 1))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 6:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def append_cache(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def correlation(xs: list[float], ys: list[float]) -> float:
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else float("nan")


def make_plot(rows: list[dict], path: Path, model: str) -> tuple[float, float]:
    import matplotlib.pyplot as plt

    xs = [row["jev_heaven_percent"] for row in rows]
    ys = [row["jeff_heaven_percent"] for row in rows]
    r = correlation(xs, ys)
    mae = sum(abs(x - y) for x, y in zip(xs, ys)) / len(xs)

    plt.style.use("seaborn-v0_8-whitegrid")
    figure, axis = plt.subplots(figsize=(9, 9), dpi=180)
    axis.scatter(xs, ys, s=22, alpha=0.42, color="#5B3FD4", edgecolors="none")
    axis.plot([0, 100], [0, 100], linestyle="--", linewidth=1.5, color="#D95F02", label="Equal %Heaven")
    axis.set(xlim=(-2, 102), ylim=(-2, 102))
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("Jev %Heaven", fontsize=12)
    axis.set_ylabel("Jeff %Heaven", fontsize=12)
    axis.set_title(f"Jev vs Jeff ({model}): Heaven Probability", fontsize=17, pad=14)
    axis.text(
        0.02,
        0.98,
        f"n = {len(rows):,} historical results\nPearson r = {r:.3f}\nMean absolute difference = {mae:.1f} points",
        transform=axis.transAxes,
        va="top",
        ha="left",
        fontsize=10,
        bbox={"boxstyle": "round,pad=0.5", "facecolor": "white", "alpha": 0.88, "edgecolor": "#BBBBBB"},
    )
    axis.legend(loc="lower right", frameon=True)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)
    return r, mae


def main() -> None:
    args = parse_args()
    history = load_history(args.history)
    cache = load_cache(args.cache)
    subjects = list(dict.fromkeys(row["subject"] for row in history))
    missing = [subject for subject in subjects if subject not in cache]

    if missing:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(evaluate, args.server, args.model, subject): subject
                for subject in missing
            }
            completed = 0
            for future in as_completed(futures):
                result = future.result()
                cache[result["subject"]] = result
                append_cache(args.cache, result)
                completed += 1
                if completed % 50 == 0 or completed == len(missing):
                    print(f"Evaluated {completed}/{len(missing)} uncached subjects", flush=True)

    output_rows = []
    for row in history:
        jeff = cache[row["subject"]]
        output_rows.append(
            {
                "sequence": row.get("sequence"),
                "subject": row["subject"],
                "created_at": row.get("created_at"),
                "jev_heaven_percent": 100 * row["probabilities"]["heaven"],
                "jeff_heaven_percent": 100 * jeff["probabilities"]["heaven"],
                "jev_choice": row["choice"],
                "jeff_choice": jeff["choice"],
                "jeff_model": jeff["model"],
            }
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)

    r, mae = make_plot(output_rows, args.plot, args.model)
    print(
        json.dumps(
            {
                "history_rows": len(history),
                "unique_subjects": len(subjects),
                "pearson_r": r,
                "mean_absolute_difference_points": mae,
                "csv": str(args.csv),
                "plot": str(args.plot),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
