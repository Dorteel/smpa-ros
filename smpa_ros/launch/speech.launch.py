"""Compatibility entry point for the central session launcher (defaults to speech)."""
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('service', default_value='speech', choices=['speech', 'vision', 'all']),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(str(Path(__file__).with_name('llm.launch.py')))),
    ])
