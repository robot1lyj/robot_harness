def tool_point_mm(slide_mm, tool_offset_mm):
    if not 0 <= slide_mm <= 400:
        raise ValueError("slide coordinate outside range")
    return [slide_mm + tool_offset_mm, 0, 120]
