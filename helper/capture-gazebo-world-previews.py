#!/usr/bin/env python3
"""Capture standardized Gazebo Classic previews for gazebo_sim_worlds."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path


DEVOPS_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = DEVOPS_ROOT / "products/ros1/simulator/gazebo-sim/scenes/gazebo_sim_worlds"
DEFAULT_CONTAINER = "xgc2-world-previews"
DEFAULT_ROS_MASTER_URI = "http://127.0.0.1:11411"
DEFAULT_GAZEBO_MASTER_URI = "http://127.0.0.1:11445"


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def shell(
    command: str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(["bash", "-lc", command], cwd=cwd, check=check, capture=capture)


def discover_worlds(selected: list[str] | None = None) -> list[Path]:
    worlds = sorted((PACKAGE_ROOT / "worlds").glob("*/*.world"))
    if not selected:
        return worlds

    wanted = set(selected)
    matched: list[Path] = []
    for world in worlds:
        if world.stem in wanted or str(world.relative_to(PACKAGE_ROOT)) in wanted:
            matched.append(world)

    missing = wanted - {world.stem for world in matched} - {
        str(world.relative_to(PACKAGE_ROOT)) for world in matched
    }
    if missing:
        raise SystemExit(f"Unknown world(s): {', '.join(sorted(missing))}")
    return matched


def start_container(args: argparse.Namespace) -> None:
    helper = DEVOPS_ROOT / "helper" / "start-ros1-container.sh"
    cmd = [
        str(helper),
        "--name",
        args.container,
        "--detach",
        "--reset",
        "--ros-master-uri",
        args.ros_master_uri,
        "--gazebo-master-uri",
        args.gazebo_master_uri,
    ]
    run(cmd, cwd=DEVOPS_ROOT)


def docker_exec(container: str, command: str, *, detach: bool = False) -> None:
    cmd = ["docker", "exec"]
    if detach:
        cmd.append("-d")
    cmd.extend([container, "bash", "-lc", command])
    run(cmd, cwd=DEVOPS_ROOT)


def visible_gazebo_windows() -> list[str]:
    result = shell(
        "xdotool search --onlyvisible --name '^Gazebo$' 2>/dev/null || true",
        capture=True,
    )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if re.fullmatch(r"[0-9]+", line.strip())
    ]


def stop_gazebo(container: str) -> None:
    docker_exec(
        container,
        "pkill -TERM -f '[r]oslaunch|[g]zserver|[g]zclient|[r]osmaster|[r]oscore' || true",
    )
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not visible_gazebo_windows():
            return
        time.sleep(0.25)
    docker_exec(
        container,
        "pkill -KILL -f '[r]oslaunch|[g]zserver|[g]zclient|[r]osmaster|[r]oscore' || true",
    )
    time.sleep(1.0)


def launch_world(container: str, world: Path) -> None:
    world_in_container = Path("/xgc2-devops") / world.relative_to(DEVOPS_ROOT)
    log_name = f"/tmp/xgc2-world-preview-{world.stem}.log"
    command = f"""
set -e
source /opt/ros/noetic/setup.bash
if [ -f /ros1_ws/devel/setup.bash ]; then source /ros1_ws/devel/setup.bash; fi
export GAZEBO_MODEL_PATH=/xgc2-devops/products/ros1/simulator/gazebo-sim/scenes/gazebo_sim_worlds/models:${{GAZEBO_MODEL_PATH:-}}
export GAZEBO_RESOURCE_PATH=/xgc2-devops/products/ros1/simulator/gazebo-sim/scenes/gazebo_sim_worlds:${{GAZEBO_RESOURCE_PATH:-}}
export GAZEBO_MODEL_DATABASE_URI=
exec roslaunch gazebo_ros empty_world.launch \\
  world_name:={world_in_container} \\
  gui:=true paused:=true verbose:=false > {log_name} 2>&1
"""
    docker_exec(container, command, detach=True)


def wait_for_gazebo_window(timeout: float) -> str:
    deadline = time.time() + timeout
    last_windows: list[str] = []
    while time.time() < deadline:
        windows = visible_gazebo_windows()
        if windows:
            return windows[-1]
        last_windows = windows
        time.sleep(0.25)
    raise RuntimeError(f"Gazebo window did not appear; last windows: {last_windows}")


def position_window(window_id: str, geometry: str) -> None:
    match = re.fullmatch(r"([0-9]+)x([0-9]+)\+(-?[0-9]+)\+(-?[0-9]+)", geometry)
    if not match:
        raise ValueError(f"Invalid window geometry: {geometry}")
    width, height, x, y = match.groups()
    shell(f"xdotool windowsize {window_id} {width} {height}", check=False)
    shell(f"xdotool windowmove {window_id} {x} {y}", check=False)
    shell(f"xdotool windowactivate {window_id}", check=False)


def capture_window(window_id: str, output: Path, size: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shell(
        f"xwd -silent -id {window_id} | "
        f"convert xwd:- -resize {size}! -strip {output}",
        cwd=PACKAGE_ROOT,
    )


def launch_command(world: Path) -> str:
    rel = world.relative_to(PACKAGE_ROOT)
    return (
        'roslaunch gazebo_ros empty_world.launch '
        f'world_name:="$(rospack find gazebo_sim_worlds)/{rel}" '
        "gui:=true paused:=true"
    )


def write_readme(worlds: list[Path]) -> None:
    readme = PACKAGE_ROOT / "README.md"
    content = readme.read_text()
    marker_start = "<!-- WORLD_CATALOG_START -->"
    marker_end = "<!-- WORLD_CATALOG_END -->"

    rows = [
        marker_start,
        "",
        "## World Preview Catalog",
        "",
        "The preview images are generated from the ROS1 Docker runtime with Gazebo GUI on the middle display. Each command below launches the installed world asset directly through `gazebo_ros`.",
        "Dynamic worlds are shown as structural snapshots; runtime obstacle motion is provided by simulator plugins outside this world asset package.",
        "",
        "To regenerate previews from the `xgc2-devops` repository root:",
        "",
        "```bash",
        "python3 helper/capture-gazebo-world-previews.py --update-readme",
        "```",
        "",
        "| World | Preview | Launch command |",
        "| --- | --- | --- |",
    ]
    for world in worlds:
        rel = world.relative_to(PACKAGE_ROOT)
        image = f"pics/{world.stem}.png"
        rows.append(
            f"| `{rel}` | <img src=\"{image}\" width=\"240\"> | "
            f"`{launch_command(world)}` |"
        )
    rows.extend(["", marker_end, ""])
    section = "\n".join(rows)

    heading = "## World Preview Catalog"
    if heading in content and marker_end in content:
        prefix = content[: content.index(heading)]
        suffix = content[content.index(marker_end) + len(marker_end) :]
        new_content = prefix.rstrip() + "\n\n" + section + suffix
    elif marker_start in content and marker_end in content:
        prefix = content[: content.index(marker_start)]
        suffix = content[content.index(marker_end) + len(marker_end) :]
        new_content = prefix.rstrip() + "\n\n" + section + suffix
    else:
        new_content = content.rstrip() + "\n\n" + section + "\n"

    readme.write_text(new_content)


def capture_worlds(args: argparse.Namespace, worlds: list[Path]) -> list[str]:
    failures: list[str] = []
    pics_dir = PACKAGE_ROOT / "pics"

    if args.start_container:
        start_container(args)

    for index, world in enumerate(worlds, start=1):
        print(f"[{index:02d}/{len(worlds):02d}] {world.relative_to(PACKAGE_ROOT)}")
        try:
            stop_gazebo(args.container)
            launch_world(args.container, world)
            time.sleep(args.settle_seconds)
            window_id = wait_for_gazebo_window(args.window_timeout)
            position_window(window_id, args.window_geometry)
            time.sleep(args.after_position_seconds)
            capture_window(window_id, pics_dir / f"{world.stem}.png", args.image_size)
        except Exception as exc:  # noqa: BLE001 - continue collecting all failures.
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures.append(world.stem)

    if args.stop_after:
        stop_gazebo(args.container)
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--ros-master-uri", default=DEFAULT_ROS_MASTER_URI)
    parser.add_argument("--gazebo-master-uri", default=DEFAULT_GAZEBO_MASTER_URI)
    parser.add_argument(
        "--window-geometry",
        default="1920x948+1080+64",
        help="Gazebo GUI geometry. Default targets the middle HDMI-0 display.",
    )
    parser.add_argument("--image-size", default="1280x632")
    parser.add_argument("--settle-seconds", type=float, default=10.0)
    parser.add_argument("--after-position-seconds", type=float, default=1.0)
    parser.add_argument("--window-timeout", type=float, default=30.0)
    parser.add_argument("--world", action="append", help="World stem or relative path")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-start-container", dest="start_container", action="store_false")
    parser.add_argument("--stop-after", action="store_true")
    parser.add_argument("--update-readme", action="store_true")
    parser.add_argument(
        "--readme-only",
        action="store_true",
        help="Only rewrite README world catalog; do not launch Gazebo.",
    )
    parser.set_defaults(start_container=True)
    args = parser.parse_args()

    if not os.environ.get("DISPLAY"):
        raise SystemExit("DISPLAY is not set; Gazebo GUI screenshots need X11.")
    return args


def main() -> int:
    args = parse_args()
    worlds = discover_worlds(args.world)
    if args.limit > 0:
        worlds = worlds[: args.limit]

    if args.readme_only:
        write_readme(discover_worlds(None))
        return 0

    failures = capture_worlds(args, worlds)
    if args.update_readme:
        write_readme(discover_worlds(None))

    if failures:
        print(f"Failed worlds: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
