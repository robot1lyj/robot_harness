"""One static catalog for CLI, MCP, native clients and runtime dispatch."""
from .features.scene import SceneTools
from .features.session import SessionTools
from .features.context import ContextTools
from .features.tasks import TaskTools
from .features.discovery import DiscoveryTools
from .features.resources import ResourceTools

FEATURES = (SceneTools, SessionTools, ContextTools, TaskTools, DiscoveryTools, ResourceTools,)
TOOLS = {name: definition for feature in FEATURES for name, definition in feature.definitions.items()}
AGENT_TOOLS = [name for feature in FEATURES for name in feature.native_tools]
MOTION_TOOLS = set().union(*(feature.motion_tools for feature in FEATURES))
OPERATION_TOOLS = set().union(*(feature.operation_tools for feature in FEATURES))
