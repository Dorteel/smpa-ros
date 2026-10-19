"""Local CPU action servers; optionally start the half-duplex demonstration."""
from pathlib import Path
import os
import sys
import tempfile

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    common = {'model_root': LaunchConfiguration('model_root'),
              'audio_lock': LaunchConfiguration('audio_lock')}
    prefix = [LaunchConfiguration('python')]
    return LaunchDescription([
        DeclareLaunchArgument('python', default_value=sys.executable),
        DeclareLaunchArgument('model_root', default_value=str(Path.cwd() / 'models')),
        DeclareLaunchArgument('audio_lock', default_value=str(Path(tempfile.gettempdir()) / f'smpa-speech-{os.getuid()}.lock')),
        DeclareLaunchArgument('input_device', default_value=''),
        DeclareLaunchArgument('output_device', default_value=''),
        DeclareLaunchArgument('demo', default_value='false', choices=['true', 'false']),
        Node(package='smpa_ros', executable='listen', prefix=prefix, output='screen',
             parameters=[common, {'audio_device': ParameterValue(LaunchConfiguration('input_device'), value_type=str)}]),
        Node(package='smpa_ros', executable='talk', prefix=prefix, output='screen',
             parameters=[common, {'audio_device': ParameterValue(LaunchConfiguration('output_device'), value_type=str)}]),
        Node(package='smpa_ros', executable='listener_talker', prefix=prefix, output='screen',
             condition=IfCondition(LaunchConfiguration('demo'))),
    ])
