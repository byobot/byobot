"""Bundle the shared contracts (schema/v0 and schema/v1) and the activity library (library/) into the wheel.

Building from a git checkout reads ../schema and ../library; building from an
sdist reads the copies the sdist carries in ./schema and ./library.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        for name, marker, target in (("schema/v0", "event.schema.json", "byobot/_schema/v0"),
                                     ("schema/v1", "event.schema.json", "byobot/_schema/v1"),
                                     ("library", "vocabulary.json", "byobot/_library")):
            found = next((c for c in (root.parent / name, root / name) if (c / marker).is_file()), None)
            if found is None:
                raise RuntimeError(f"{name} not found; build from a byobot checkout or sdist")
            build_data["force_include"][str(found)] = target
