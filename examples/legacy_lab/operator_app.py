"""Old manual workflow. Reading this file must not start the application."""


def align_and_move():
    raise NotImplementedError("Operator alignment, collision checking and device owner missing")


if __name__ == "__main__":
    raise RuntimeError("Manual station setup is required; do not start during discovery")
