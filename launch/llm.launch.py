"""Start the loop, or start its explicitly assigned operation servers."""
from pathlib import Path
import os
import sys
import tempfile

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, OpaqueFunction
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def start(context):
    value = lambda name: LaunchConfiguration(name).perform(context)
    if value('participants_only') == 'true':
        from smpa_ros.loop import assigned_run
        directory, identity = assigned_run(value('world_model_dir'), value('run_id'))
        namespace = '/run_' + identity.replace('-', '_')
        common = {'run_id': identity, 'world_model_dir': str(directory)}
        remaps = [(endpoint, namespace + endpoint)
                  for endpoint in ('/sense', '/speech/listen', '/speech/talk')]
        nodes = []
        if value('service') in ('vision', 'all'):
            nodes.append(Node(package='smpa_ros', executable='sense', prefix=[value('python')],
                              parameters=[dict(common, config=value('config'))], remappings=remaps,
                              output='screen', on_exit=EmitEvent(event=Shutdown(reason='Sense server exited'))))
        if value('service') in ('speech', 'all'):
            for executable, device in [('listen', 'input_device'), ('talk', 'output_device')]:
                parameters = dict(common, model_root=value('model_root'), audio_lock=value('audio_lock'),
                                  audio_device=ParameterValue(value(device), value_type=str))
                nodes.append(Node(package='smpa_ros', executable=executable, prefix=[value('python')],
                                  parameters=[parameters], remappings=remaps, output='screen',
                                  on_exit=EmitEvent(event=Shutdown(reason='Speech server exited'))))
        return nodes
    arguments = []
    for name in ('config', 'world_model_root', 'run_name', 'service', 'python', 'model_root',
                 'audio_lock', 'input_device', 'output_device', 'demo'):
        if value(name):
            arguments.extend(['--' + name.replace('_', '-'), value(name)])
    if value('max_cycles'):
        arguments.extend(['--max-cycles', value('max_cycles')])
    return [Node(package='smpa_ros', executable='loop', prefix=[value('python')],
                 arguments=arguments, output='screen',
                 on_exit=EmitEvent(event=Shutdown(reason='Loop process exited')))]


def generate_launch_description():
    share = Path(get_package_share_directory('smpa_ros'))
    return LaunchDescription([
        DeclareLaunchArgument('service', default_value='vision', choices=['speech', 'vision', 'all']),
        DeclareLaunchArgument('run_name', default_value='session'),
        DeclareLaunchArgument('max_cycles', default_value='', description='Override execution.max_cycles'),
        DeclareLaunchArgument('world_model_root', default_value=str(Path.cwd() / 'world_model')),
        DeclareLaunchArgument('config', default_value=str(share / 'config/pipeline.yaml')),
        DeclareLaunchArgument('python', default_value=sys.executable),
        DeclareLaunchArgument('model_root', default_value=str(Path.cwd() / 'models')),
        DeclareLaunchArgument('audio_lock', default_value=str(Path(tempfile.gettempdir()) / f'smpa-speech-{os.getuid()}.lock')),
        DeclareLaunchArgument('input_device', default_value=''),
        DeclareLaunchArgument('output_device', default_value=''),
        DeclareLaunchArgument('demo', default_value='false', choices=['true', 'false']),
        DeclareLaunchArgument('participants_only', default_value='false', choices=['true', 'false']),
        DeclareLaunchArgument('world_model_dir', default_value=''),
        DeclareLaunchArgument('run_id', default_value=''),
        OpaqueFunction(function=start),
    ])
