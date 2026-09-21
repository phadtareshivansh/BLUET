# -*- coding: utf-8 -*-
"""Function performing a file read; flagged as non-deterministic."""


def load_prices(path):
    rows = []
    with open(path, "rb") as handle:
        for line in handle.readlines():
            rows.append(line.strip())
    return rows