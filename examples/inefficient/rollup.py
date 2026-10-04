"""Row-wise pandas and nested loops that will not scale."""

from pathlib import Path

import pandas as pd


def slow_customer_rollup(path: Path) -> list[str]:
    frame = pd.read_csv(path)
    labels = []
    for _, row in frame.iterrows():
        labels.append(row["name"] + "-" + str(row["id"]))

    scores = frame["amount"].apply(lambda value: value * 1.2 if value else 0)

    names = ""
    for label in labels:
        names += label + ","

    pairs = []
    for left in labels:
        for right in labels:
            if left != right:
                pairs.append((left, right))

    payload = path.read_text()
    return [names, payload[:10], str(scores.sum()), str(len(pairs))]
