"""Synthetic Cartesian joints over the existing mock owner; not a physical robot model."""

import time

from .capabilities import Capability
from .contracts import HarnessError, VersionRef
from .perception import CaptureTime
from .resources import CommandFeedback, CommandReceipt, Joint, RobotModel, RobotState, Tool
from .storage import uid


class MockDevice:
    def __init__(self, owner):
        self.owner = owner
        self.model = RobotModel(
            reference=VersionRef(id="mock_cartesian", version="v1"),
            resource_id="mock_robot",
            joints=[
                Joint(id=arm + "_" + axis, kind="prismatic", lower=lo, upper=hi)
                for arm in owner.arms
                for axis, lo, hi in [("x", 0.1, 0.6), ("y", -0.3, 0.3), ("z", 0.02, 0.4)]
            ],
            manipulators={arm: [arm + "_" + axis for axis in "xyz"] for arm in owner.arms},
            tools=[
                Tool(
                    id=arm + "_gripper",
                    manipulator=arm,
                    commands=["opening"],
                    unit="fraction",
                    lower=0,
                    upper=1,
                )
                for arm in owner.arms
            ],
            frames=["mock_table"],
            source="synthetic Cartesian fixture, not measured joints",
            qualification="synthetic",
        )
        self.capabilities = tuple(
            Capability(
                name=name,
                contract_version=1,
                provider_id="mock_device",
                implementation_version="1",
                qualification="synthetic",
            )
            for name in ["robot.state", "robot.command"]
        )
        self.pending = {}

    async def connect(self):
        await self.owner.connect()

    async def close(self):
        await self.owner.close()

    async def hold(self):
        return await self.owner.hold()

    async def state(self):
        if not self.owner.connected:
            raise HarnessError("resource_not_connected", "Explicitly connect the device resource")
        return RobotState(
            id=uid("state"),
            resource_id="mock_robot",
            model_ref=self.model.reference,
            joints={
                arm + "_" + axis: value
                for arm, point in self.owner.poses.items()
                for axis, value in zip("xyz", point)
            },
            tools={arm + "_gripper": v for arm, v in self.owner.openings.items()},
            capture_time=CaptureTime(
                value_s=time.time(), kind="unix", clock_domain="utc", uncertainty_s=0
            ),
            received_at=time.time(),
            boot_id=self.owner.boot_id,
            epoch=self.owner.epoch,
            source="mock_owner_state",
            qualification="synthetic",
        )

    async def submit(self, command, operation_id):
        if not self.owner.connected:
            raise HarnessError("resource_not_connected", "Explicitly connect the device resource")
        receipt = CommandReceipt(
            command_id=uid("command"),
            operation_id=operation_id,
            request_id=command.request_id,
            resource_id=command.resource_id,
            status="accepted",
            boot_id=self.owner.boot_id,
            epoch=self.owner.epoch,
            source="mock_owner",
            qualification="synthetic",
        )
        self.pending[receipt.command_id] = command
        return receipt

    async def wait(self, receipt):
        command = self.pending.pop(receipt.command_id)
        if command.trajectory:
            for point in command.trajectory:
                await self.owner.move(
                    command.arm, [point.positions[command.arm + "_" + axis] for axis in "xyz"]
                )
        else:
            await self.owner.gripper(command.arm, command.effector.value)
        return CommandFeedback(
            command_id=receipt.command_id,
            operation_id=receipt.operation_id,
            status="completed",
            state=await self.state(),
            source="mock_state_feedback",
            qualification="synthetic",
        )
