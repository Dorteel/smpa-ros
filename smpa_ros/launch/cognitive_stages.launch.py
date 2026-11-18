"""Start the independently callable cognitive stages without an experiment."""
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Launch the four stage servers with a shared run root and inference node.

    Each node exposes only its own action and starts no other cognitive stage.
    The inference server and any experiment remain separate processes. Operators
    may override the memory root, inference endpoint and bounded stage timeout.
    """
    root = LaunchConfiguration('memory_root')
    inference = LaunchConfiguration('inference_node')
    timeout = LaunchConfiguration('timeout')
    parameters = {'memory_root': root, 'inference_node': inference,
                  'timeout': ParameterValue(timeout, value_type=float)}
    return LaunchDescription([
        DeclareLaunchArgument('memory_root', default_value=str(Path.cwd() / 'memory')),
        DeclareLaunchArgument('inference_node', default_value='/llm'),
        DeclareLaunchArgument('timeout', default_value='120.0'),
        *[Node(package='smpa_ros', executable=stage + '_stage', parameters=[parameters], output='screen')
          for stage in ('sense', 'model', 'plan', 'act')],
    ])
