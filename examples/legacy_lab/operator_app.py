"""Old manual workflow. Reading this file must not start the application."""


def align_and_move():
    raise NotImplementedError("Operator alignment, collision checking and device owner missing")


def inspect_height(ui, view, position_mm, lower_mm, upper_mm):
    """Existing manual check: UI supplies inputs and displays the measured interval decision."""
    from .capture import read_recording
    from .geometry import tip_mm

    if lower_mm > upper_mm:
        raise ValueError("lower height must not exceed upper height")
    record = read_recording(view)
    point = tip_mm(position_mm, record["height_mm"])
    result = {"point_mm": point, "within_bounds": lower_mm <= point[2] <= upper_mm}
    ui.show(result)
    return result


if __name__ == "__main__":
    raise RuntimeError("Manual station setup is required; do not start during discovery")
