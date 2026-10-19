"""Start exactly one selected inference service."""
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def start_service(context):
    service = LaunchConfiguration('service').perform(context)
    model = LaunchConfiguration('model').perform(context)
    parameters = {}
    if service == 'sense':
        if model:
            parameters['model_config'] = model if model.endswith('.yaml') else model + '.yaml'
    else:
        parameters = {'model': model or 'visual-jev-4b',
                      'device': LaunchConfiguration('device').perform(context),
                      'model_root': LaunchConfiguration('model_root').perform(context),
                      'source_dir': LaunchConfiguration('source_dir').perform(context)}
    return [Node(package='smpa_ros', executable=service, parameters=[parameters],
                 prefix=[LaunchConfiguration('python')], output='screen')]


def generate_launch_description():
    from pathlib import Path
    return LaunchDescription([
        DeclareLaunchArgument('service', default_value='sense', choices=['sense', 'visual_jev']),
        DeclareLaunchArgument('model', default_value=''),
        DeclareLaunchArgument('device', default_value='cuda', choices=['cpu', 'cuda']),
        DeclareLaunchArgument('python', default_value=sys.executable),
        DeclareLaunchArgument('model_root', default_value=str(Path.cwd() / 'models')),
        DeclareLaunchArgument('source_dir', default_value=str(Path.cwd() / 'third_party/Visual-Jev/code')),
        OpaqueFunction(function=start_service),
    ])
