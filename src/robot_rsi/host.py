"""Small composition root for one project; no independent camera/depth deployment framework."""

from __future__ import annotations

from .cameras import Cameras
from .contracts import Contract, HostConfig, Profile
from .depth import FastFoundationStereo
from .depth_backends import FFSDepth
from .robots import LegacyYam, MockRobot


def resolve_host(profile: Profile) -> HostConfig:
    return HostConfig.model_validate(profile.host.model_dump())


def build_cameras(config: HostConfig, store, robot):
    return Cameras(robot, config.cameras, store)


def build_depth(config: HostConfig, store, *, predictor=None):
    return FFSDepth(
        store, predictor or FastFoundationStereo(config.depth), config.depth.model_dump()
    )


def _builtin_adapter(context, legacy=False):
    from dataclasses import replace

    from .adapters import Adapter
    from .builtin_device import MockDevice
    from .depth import Depth
    from .resources import Resource
    from .skill_adapters import built_in_bindings

    profile, store = context.profile, context.store
    config = resolve_host(profile)
    owner = (
        LegacyYam(profile, store, transport=context.robot_transport)
        if legacy
        else MockRobot(profile, store)
    )
    camera = None
    provider = None

    async def observe(camera_role=None):
        return await capture_scene(owner, camera, store, camera_role)

    bindings = (context.bindings_factory or built_in_bindings)(owner, store, observe)
    injected_camera = bindings.camera is not None
    camera = bindings.camera or (build_cameras(config, store, owner) if config.cameras else None)
    if bindings.depth is None and config.depth:
        provider = build_depth(config, store, predictor=context.depth_predictor)
    depth = bindings.depth or (Depth(store, provider) if provider else None)
    bindings = replace(
        bindings,
        camera=camera,
        depth=depth,
        robot=bindings.robot or (None if legacy else MockDevice(owner)),
    )
    resources = [
        Resource(
            resource_id=owner.provider_id,
            kind="robot",
            roles=list(owner.arms),
            provider=owner.provider_id,
            implementation_version="1",
            capabilities=[c.name for c in bindings.robot.capabilities]
            if bindings.robot
            else ["scene.observe"],
            qualification=owner.qualification,
            frames=list(owner.frames),
            owner_id="device",
            model_ref=bindings.robot.model.reference if bindings.robot else None,
        )
    ]
    if bindings.camera:
        resources.append(
            Resource(
                resource_id="camera",
                kind="camera",
                roles=[c["role"] for c in bindings.camera.describe()["cameras"]],
                provider=owner.provider_id,
                implementation_version="1",
                capabilities=["camera.describe", "camera.capture"],
                qualification=owner.qualification,
                owner_id=None if injected_camera else "device",
            )
        )
    return Adapter(
        bindings,
        tuple(resources),
        owner,
        {"device": owner, **({"depth": provider} if provider else {})},
        models=(bindings.robot.model,) if bindings.robot else (),
        depth_provider=provider,
    )


def mock_adapter(context):
    return _builtin_adapter(context)


def legacy_adapter(context):
    return _builtin_adapter(context, legacy=True)


mock_adapter.config_model = Contract
legacy_adapter.config_model = Contract


async def capture_scene(owner, camera, store, role=None):
    """The built-in scene adapter joins old scene state and optional camera references."""
    from .contracts import HarnessError

    if camera is not None:
        role = role or camera.default_camera
        if role not in {item["role"] for item in camera.describe()["cameras"]}:
            raise HarnessError("unsupported_camera", "Semantic camera role is not configured")
    elif role is not None:
        raise HarnessError("unsupported_camera", "No camera configured")
    result = await owner.read_state()
    result.update(
        camera=role,
        image_artifact=None,
        stereo=None,
        camera_observation_id=None,
        state_camera_synchronized=False,
    )
    if camera is not None:
        try:
            captured = await camera.capture(role)
            result["camera_observation_id"] = captured.id
            result["image_artifact"] = captured.frames[0].artifact_id if captured.frames else None
            if pair := captured.stereo:
                result["stereo"] = {
                    "left_artifact": captured.image(pair.left_stream).artifact_id,
                    "right_artifact": captured.image(pair.right_stream).artifact_id,
                    "calibration": pair.calibration.model_dump(),
                }
        except HarnessError as exc:
            result["camera_error"] = exc.payload()
    with store.transaction():
        store.put("observation", result["id"], result)
        store.event("robot.observed", {"id": result["id"], "source": result["source"]})
    return result
