# -*- coding: utf-8 -*-
"""Function combining a loop and a branch over a list of records."""


def apply_surcharge(records, premium):
    running = 0
    for r in records:
        if r["premium"]:
            running += r["surcharge"]
        else:
            running += r["base"]
    while running < 1000:
        running += 1
    return running