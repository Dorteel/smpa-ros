"""Launch the opt-in LLM server independently of the cognitive experiment."""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def launch_server(context):
    """Resolve optional overrides without treating an empty filename as a YAML file.

    Read a supplied ROS parameter file first. Add a launch model selection only
    when explicitly nonempty, so a model selected in that file remains effective.
    An empty selection with no file leaves the new server unloaded. Namespace
    selection affects all relative endpoints without touching the legacy launch.
    """
    parameters = []
    filename = LaunchConfiguration('params_file').perform(context)
    initial = LaunchConfiguration('active_model').perform(context)
    if filename:
        parameters.append(filename)
    if initial:
        parameters.append({'active_model': initial})
    return [Node(package='smpa_llm', executable='llm_server', output='screen',
                 namespace=LaunchConfiguration('namespace'), parameters=parameters)]


def generate_launch_description():
    """Expose explicit model selection, optional ROS parameter YAML and namespace.

    Construct only the independent inference node. The existing cognitive launch
    keeps its own in-process inference path and never requires this executable.
    """
    return LaunchDescription([
        DeclareLaunchArgument('namespace', default_value=''),
        DeclareLaunchArgument('active_model', default_value=''),
        DeclareLaunchArgument('params_file', default_value=''),
        OpaqueFunction(function=launch_server),
    ])
