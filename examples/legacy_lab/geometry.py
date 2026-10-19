"""Existing slider calculation uses millimetres, without a device connection."""


def tip_mm(position_mm, height_mm):
    if not 0 <= position_mm <= 1000:
        raise ValueError("slider outside recorded range")
    return [position_mm, 0, height_mm]
