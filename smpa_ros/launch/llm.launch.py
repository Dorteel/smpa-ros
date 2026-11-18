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
    """Start the selected workflow without coupling visual evaluation to cognition.

    Explicit VisualJEV selection starts only its independent evaluator, with its
    pixel-budget argument parsed as an integer ROS parameter. All other
    selections retain the existing robot loop or assigned participant servers,
    including modality-scoped run remappings and unchanged action execution.
    Place Listen under sense/hear and Talk under act; forward explicit modality
    switches and the selected Read session to the experiment. Unless disabled,
    start the four independent stage servers with the same run-memory root.
    """
    value = lambda name: LaunchConfiguration(name).perform(context)
    if value('service') == 'visual_jev':
        parameters = {'model': value('model'), 'model_root': value('model_root'),
                      'source_dir': value('source_dir'), 'device': value('device'),
                      'max_pixels': int(value('max_pixels'))}
        return [Node(package='smpa_ros', executable='visual_jev', parameters=[parameters],
                     prefix=[value('python')], output='screen')]
    if value('participants_only') == 'true':
        from memory import MemoryManager
        directory, identity = MemoryManager.assigned_run(value('world_model_dir'), value('run_id'))
        namespace = '/run_' + identity.replace('-', '_')
        common = {'run_id': identity, 'world_model_dir': str(directory)}
        remaps = [('/sense', namespace + '/sense/vision/describe_scene'),
                  ('/speech/listen', namespace + '/sense/hear/listen'),
                  ('/speech/talk', namespace + '/act/talk')]
        nodes = []
        if value('service') in ('vision', 'all'):
            nodes.append(Node(package='smpa_ros', executable='sense', namespace=namespace + '/sense/vision', prefix=[value('python')],
                              parameters=[dict(common, config=value('config'))], remappings=remaps,
                              output='screen', on_exit=EmitEvent(event=Shutdown(reason='Sense server exited'))))
        if value('service') in ('speech', 'all'):
            for executable, device in [('listen', 'input_device'), ('talk', 'output_device')]:
                parameters = dict(common, model_root=value('model_root'), audio_lock=value('audio_lock'),
                                  audio_device=ParameterValue(value(device), value_type=str))
                nodes.append(Node(package='smpa_ros', executable=executable,
                                  namespace=namespace + ('/sense/hear' if executable == 'listen' else '/act'), prefix=[value('python')],
                                  parameters=[parameters], remappings=remaps, output='screen',
                                  on_exit=EmitEvent(event=Shutdown(reason='Speech server exited'))))
        return nodes
    arguments = []
    for name in ('config', 'world_model_root', 'run_name', 'service', 'python', 'model_root',
                 'audio_lock', 'input_device', 'output_device', 'demo', 'timeout', 'hear', 'vision', 'read_session'):
        if value(name):
            arguments.extend(['--' + name.replace('_', '-'), value(name)])
    if value('max_cycles'):
        arguments.extend(['--max-cycles', value('max_cycles')])
    stages = []
    if value('start_stage_nodes') == 'true':
        parameters = {'memory_root': value('world_model_root'),
                      'timeout': float(value('timeout') or '120.0')}
        for stage in ('sense', 'model', 'plan', 'act'):
            stages.append(Node(package='smpa_ros', executable=stage + '_stage',
                               prefix=[value('python')], parameters=[parameters], output='screen',
                               on_exit=EmitEvent(event=Shutdown(reason=stage + ' stage exited'))))
    return stages + [Node(package='smpa_ros', executable='smpa', prefix=[value('python')],
                          arguments=arguments, output='screen',
                          on_exit=EmitEvent(event=Shutdown(reason='Loop process exited')))]


def generate_launch_description():
    """Declare existing robot defaults and optional independent evaluator settings.

    Keep vision as the default robot service. VisualJEV model, device, source and
    pixel-budget arguments are used only when that service is explicitly selected.
    Return the launch description whose existing callback starts the chosen path.
    """
    share = Path(get_package_share_directory('smpa_ros'))
    return LaunchDescription([
        DeclareLaunchArgument('service', default_value='vision', choices=['speech', 'vision', 'all', 'visual_jev']),
        DeclareLaunchArgument('model', default_value='visual-jev-4b'),
        DeclareLaunchArgument('device', default_value='cuda', choices=['cpu', 'cuda']),
        DeclareLaunchArgument('source_dir', default_value=str(Path.cwd() / 'third_party/Visual-Jev/code')),
        DeclareLaunchArgument('max_pixels', default_value='200704'),
        DeclareLaunchArgument('hear', default_value=''),
        DeclareLaunchArgument('vision', default_value=''),
        DeclareLaunchArgument('read_session', default_value=''),
        DeclareLaunchArgument('run_name', default_value='smpa'),
        DeclareLaunchArgument('max_cycles', default_value='', description='Override execution.max_cycles'),
        DeclareLaunchArgument('timeout', default_value='', description='Override camera/Sense wait timeout in seconds (default: 120)'),
        DeclareLaunchArgument('world_model_root', default_value=str(Path.cwd() / 'memory')),
        DeclareLaunchArgument('config', default_value=str(share / 'config.yaml')),
        DeclareLaunchArgument('python', default_value=sys.executable),
        DeclareLaunchArgument('model_root', default_value=str(Path.cwd() / 'models')),
        DeclareLaunchArgument('audio_lock', default_value=str(Path(tempfile.gettempdir()) / f'smpa-speech-{os.getuid()}.lock')),
        DeclareLaunchArgument('input_device', default_value=''),
        DeclareLaunchArgument('output_device', default_value=''),
        DeclareLaunchArgument('demo', default_value='false', choices=['true', 'false']),
        DeclareLaunchArgument('start_stage_nodes', default_value='true', choices=['true', 'false']),
        DeclareLaunchArgument('participants_only', default_value='false', choices=['true', 'false']),
        DeclareLaunchArgument('world_model_dir', default_value=''),
        DeclareLaunchArgument('run_id', default_value=''),
        OpaqueFunction(function=start),
    ])
