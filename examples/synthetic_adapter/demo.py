"""Public offline exercise. Run after installing Harness and this independent adapter."""

import argparse
import asyncio
import json
from pathlib import Path

from robot_rsi.contracts import Profile
from robot_rsi.runtime import Runtime


async def run(args):
    profile = Profile.model_validate_json(Path(args.profile).read_text())
    profile = profile.model_copy(update={"state_root": args.state_root})
    async with Runtime(profile) as runtime:
        result = await runtime.invoke("resources_list")
        for resource in result["result"]["resources"]:
            if resource["owner_id"]:
                reply = await runtime.invoke(
                    "resource_connect", {"resource_id": resource["resource_id"]}
                )
                assert reply["ok"], reply
        output = {
            "resources": result["result"],
            "qualification": "synthetic; no hardware SDK/model",
        }
        if runtime.bindings.robot:
            state = await runtime.invoke("robot_state", {"resource_id": "bench"})
            assert state["ok"], state
            op = await runtime.invoke(
                "robot_command",
                {
                    "request_id": "example-command",
                    "resource_id": "bench",
                    "observation_id": state["result"]["id"],
                    "model_ref": state["result"]["model_ref"],
                    "arm": "bridge_3",
                    "trajectory": [
                        {
                            "time_from_start_s": 0.01,
                            "positions": {"carriage_x": 0.3, "carriage_y": 0.2, "turntable": 0.5},
                        }
                    ],
                },
            )
            assert op["ok"], op
            output["command"] = await runtime.operations.get(op["result"]["id"], 2)
            assert output["command"]["status"] == "succeeded"
        if runtime.cameras:
            output["camera"] = await runtime.invoke("camera_capture", {"camera": "inspection"})
            assert output["camera"]["ok"]
        if runtime.bindings.kinematics:
            output["fk"] = await runtime.invoke(
                "kinematics_fk",
                {
                    "model_ref": profile.host.model_ref.model_dump(),
                    "arm": "bridge_3",
                    "joints": {"carriage_x": 0.2, "carriage_y": 0.1, "turntable": 0.5},
                    "base_frame": "world",
                    "target_frame": "tool_tip",
                },
            )
            assert output["fk"]["ok"], output["fk"]
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=str(Path(__file__).parent / "profiles/shared.json"))
    parser.add_argument("--state-root", required=True)
    asyncio.run(run(parser.parse_args()))
