from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-visual-description-boundaries.py"
SPEC = importlib.util.spec_from_file_location("visual_description_boundaries", SCRIPT)
assert SPEC and SPEC.loader
boundaries = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = boundaries
SPEC.loader.exec_module(boundaries)


class VisualDescriptionBoundaryTests(unittest.TestCase):
    @staticmethod
    def write_model(root: Path, body: str) -> Path:
        package = root / "products/ros1/robot/scout_description"
        (package / "urdf").mkdir(parents=True)
        (package / "urdf/scout_visual.urdf").write_text(body, encoding="utf-8")
        return package

    def test_accepts_visual_only_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_model(
                root,
                '<robot name="scout"><link name="base"><visual/></link></robot>',
            )

            self.assertEqual(boundaries.validate(root), [])

    def test_rejects_behavioral_directories_and_urdf_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.write_model(
                root,
                '<robot name="scout"><link name="base"><inertial/></link>'
                '<gazebo><plugin name="drive"/></gazebo></robot>',
            )
            (package / "launch").mkdir()
            (package / "launch/bringup.launch").write_text("<launch/>", encoding="utf-8")

            errors = boundaries.validate(root)

        self.assertTrue(any("contains launch/" in error for error in errors), errors)
        self.assertTrue(any("gazebo, inertial, plugin" in error for error in errors), errors)

    def test_rejects_non_visual_model_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.write_model(root, '<robot name="scout"/>')
            (package / "urdf/control.xacro").write_text("<robot/>", encoding="utf-8")

            errors = boundaries.validate(root)

        self.assertTrue(any("only *_visual.urdf" in error for error in errors), errors)

    def test_requires_noetic_and_jazzy_assets_to_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            noetic = self.write_model(root, '<robot name="scout"/>')
            jazzy = root / "products/ros2/robot/scout_description"
            (jazzy / "urdf").mkdir(parents=True)
            (jazzy / "urdf/scout_visual.urdf").write_text(
                '<robot name="different"/>', encoding="utf-8"
            )

            errors = boundaries.validate(root)

        self.assertTrue(any("Noetic/Jazzy visual assets differ" in error for error in errors))

    def test_requires_scout_melodic_assets_to_match_noetic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_model(root, '<robot name="scout"/>')
            melodic = root / "products/ros1/robot/melodic/scout_description"
            (melodic / "urdf").mkdir(parents=True)
            (melodic / "urdf/scout_visual.urdf").write_text(
                '<robot name="different"/>', encoding="utf-8"
            )

            errors = boundaries.validate(root)

        self.assertTrue(
            any("Noetic/Melodic visual assets differ" in error for error in errors), errors
        )

    def test_rejects_melodic_support_for_other_managed_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_model(root, '<robot name="scout"/>')
            melodic = root / "products/ros1/robot/melodic/fs150_description"
            (melodic / "urdf").mkdir(parents=True)
            (melodic / "urdf/fs150_visual.urdf").write_text(
                '<robot name="fs150"/>', encoding="utf-8"
            )

            errors = boundaries.validate(root)

        self.assertTrue(
            any("Melodic support is restricted to scout_description" in error for error in errors),
            errors,
        )

    def test_complete_matrix_requires_only_scout_on_melodic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_model(root, '<robot name="scout"/>')

            errors = boundaries.validate(root, require_complete=True)

        self.assertTrue(
            any("melodic/scout_description" in error and "is missing" in error for error in errors),
            errors,
        )
        self.assertFalse(
            any("melodic/fs150_description" in error and "is missing" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
