from capture import load_sample
from geometry import tool_point_mm


def preview(sample_path, slide_mm, tool_offset_mm):
    return {"sample": load_sample(sample_path), "tip_mm": tool_point_mm(slide_mm, tool_offset_mm)}


def main():
    raise NotImplementedError("Device transport and command arbitration are not configured")


if __name__ == "__main__":
    main()
