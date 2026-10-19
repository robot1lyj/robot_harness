"""Thin installed adaptation of legacy_lab capture/calculation; no operator entry import."""

import io
import time

from legacy_lab.capture import read_recording
from legacy_lab.geometry import tip_mm
from PIL import Image

from robot_rsi.adapters import Adapter
from robot_rsi.cameras import image_frame, save_camera
from robot_rsi.capabilities import Capability, SkillBindings
from robot_rsi.contracts import Contract, HarnessError, Intrinsics, VersionRef
from robot_rsi.perception import CameraObservation, CaptureTime
from robot_rsi.resources import CalibrationRecord, FKResult, Joint, Resource, RobotModel, Transform
from robot_rsi.scene_bindings import Candidate, CandidateInfo
from robot_rsi.storage import uid


class Options(Contract):
    default_ports: bool = True
    detail_camera_revision: str = "1"
    detail_geometry_revision: str = "1"
    survey_revision: str = "1"
    survey_description: str = "Wide survey recording, lower resolution and nominal geometry"


def capabilities(provider, names, version):
    return tuple(
        Capability(
            name=name,
            contract_version=1,
            provider_id=provider,
            implementation_version=version,
            qualification="synthetic_recording",
        )
        for name in names
    )


class RecordedCamera:
    def __init__(self, store, view, revision):
        self.store, self.view = store, view
        self.default_camera = "sample"
        self.capabilities = capabilities(
            "legacy_recording_" + view, ["camera.describe", "camera.capture"], revision
        )
        self.calls = 0

    def describe(self):
        data = read_recording(self.view)
        return {
            "default_camera": "sample",
            "cameras": [
                {
                    "role": "sample",
                    "inputs": ["preview"],
                    "resolution": [data["width"], data["height"]],
                    "source": self.view,
                }
            ],
        }

    async def capture(self, camera=None, require="any", max_age_s=None):
        if camera not in (None, "sample") or require not in ("any", "preview"):
            raise HarnessError("unsupported_camera_input", "Recording provides sample preview only")
        # These old recordings cannot meet a requested live acquisition age.
        data = read_recording(self.view)
        if max_age_s is not None and time.time() - data["captured_at"] > max_age_s:
            raise HarnessError("stale_observation", "Recorded source does not satisfy live age")
        self.calls += 1
        image = io.BytesIO()
        Image.new("RGB", (data["width"], data["height"]), tuple(data["rgb"])).save(
            image, format="PNG"
        )
        intrinsics = Intrinsics(
            width=data["width"],
            height=data["height"],
            fx=10,
            fy=10,
            cx=data["width"] / 2,
            cy=data["height"] / 2,
        )
        frame = image_frame(
            self.store,
            image.getvalue(),
            "rgb",
            frame_id="lab_optical",
            capture_time=CaptureTime(
                value_s=data["captured_at"], kind="unix", clock_domain="utc", uncertainty_s=0
            ),
            intrinsics=intrinsics,
        )
        return save_camera(
            self.store,
            CameraObservation(
                id=uid("camera_obs"),
                camera="sample",
                source_role=self.view,
                source="legacy_recording_" + self.view,
                qualification="synthetic_recording",
                received_at=time.time(),
                frames=[frame],
            ),
        )


class Geometry:
    def __init__(self, view, models, revision):
        self.view, self.models = view, {m.reference.version: m for m in models}
        self.capabilities = capabilities("legacy_geometry_" + view, ["kinematics.fk"], revision)
        self.calls = 0

    async def fk(self, query):
        if (query.base_frame, query.target_frame) != ("lab", "tip"):
            raise HarnessError("unsupported_frame", "Known slider calculation uses lab <- tip")
        self.calls += 1
        model = self.models[query.model_ref.version]
        point = tip_mm(query.joints["slide"] * 1000, model.parameters["height_m"] * 1000)
        x, y, z = [v / 1000 for v in point]
        return FKResult(
            model_ref=query.model_ref,
            pose=Transform(
                target_frame="lab",
                source_frame="tip",
                matrix=((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z), (0, 0, 0, 1)),
            ),
            source="legacy_geometry_" + self.view + ":tip_mm; mm converted to m",
            qualification="synthetic_known_formula",
        )


def factory(context):
    candidates, resources, models, calibrations = [], [], [], []
    for view in ("detail", "survey"):
        data = read_recording(view)
        geometry_revision = (
            context.options.detail_geometry_revision
            if view == "detail"
            else context.options.survey_revision
        )
        camera_revision = (
            context.options.detail_camera_revision
            if view == "detail"
            else context.options.survey_revision
        )
        ref = VersionRef(id=view + "_intrinsics", version="1")
        calibration = CalibrationRecord(
            reference=ref,
            kind="intrinsics",
            resource_id=view + "_recording",
            stream_id="rgb",
            resolution=(data["width"], data["height"]),
            intrinsics=Intrinsics(
                width=data["width"],
                height=data["height"],
                fx=10,
                fy=10,
                cx=data["width"] / 2,
                cy=data["height"] / 2,
            ),
            method="authored fixture parameters",
            sample_source=data["source"],
            qualification="synthetic",
        )
        calibrations.append(calibration)
        view_models = [
            RobotModel(
                reference=VersionRef(id=view + "_slider", version=version),
                resource_id="offline_slider",
                joints=[Joint(id="slide", kind="prismatic", lower=0, upper=1)],
                manipulators={"slider": ["slide"]},
                frames=["lab", "tip"],
                parameters={"height_m": (data["height_mm"] + delta) / 1000},
                source="legacy_lab recording dimensions; v2 is an authored calibration update",
                qualification="synthetic",
            )
            for version, delta in (("1", 0), ("2", 10))
        ]
        models.extend(view_models)
        resource = Resource(
            resource_id=view + "_recording",
            kind="camera",
            roles=["sample"],
            provider="legacy_recording_" + view,
            implementation_version=camera_revision,
            capabilities=["camera.describe", "camera.capture"],
            qualification="synthetic_recording",
            frames=["lab_optical"],
        )
        resources.append(resource)
        camera = RecordedCamera(context.store, view, camera_revision)
        geometry = Geometry(view, view_models, geometry_revision)
        description = (
            "Detail 12x8 recording and measured-fixture height 250 mm"
            if view == "detail"
            else context.options.survey_description
        )
        for role, bound, revision, refs, calrefs, resource_ids, probes in (
            (
                "camera",
                SkillBindings(None, camera=camera),
                camera_revision,
                [],
                [ref],
                [resource.resource_id],
                ("camera_capture",),
            ),
            (
                "kinematics",
                SkillBindings(None, kinematics=geometry),
                geometry_revision,
                [m.reference for m in view_models],
                [],
                [],
                ("kinematics_fk",),
            ),
        ):
            candidates.append(
                Candidate(
                    CandidateInfo(
                        reference=VersionRef(id=view + "_" + role, version=revision),
                        description=description,
                        distribution="robot-rsi-lab-adapter",
                        resource_ids=resource_ids,
                        model_refs=refs,
                        calibration_refs=calrefs,
                        conditions={"dataset": "authored_lab"},
                    ),
                    bound,
                    probes,
                )
            )
    # A host may offer candidates without selecting any default ports.
    return Adapter(
        SkillBindings(
            None,
            camera=candidates[2].bindings.camera if context.options.default_ports else None,
            kinematics=candidates[3].bindings.kinematics if context.options.default_ports else None,
        ),
        tuple(resources),
        models=tuple(models),
        calibrations=tuple(calibrations),
        candidates=tuple(candidates),
    )


factory.config_model = Options
