"""Launch the SMPA-ROS browser GUI as an independent ROS 2 participant.

Concepts:
    The GUI is one process: a local web server and one ROS 2 node
    (``/smpa_gui``). This launch description starts exactly that process and
    nothing else, so it can be used alone or included by a combined
    launcher (see README.md, "Including the GUI in another launch file").

    Launch arguments:
        host                 listening address (default 127.0.0.1, this computer only)
        port                 listening port (default 8765)
        read_only            true disables action goals, parameter changes and service calls
        keep_goals_on_exit   true leaves active action goals running when the GUI stops
        smpa                 false disables the optional SMPA integration (SMPA tab shows "disabled")
        smpa_memory_root     SMPA's memory directory for the SMPA tab (read-only); empty means
                             "memory/ beside the source checkout, if any"

Usage:
    ros2 launch smpa_gui gui.launch.py
    ros2 launch smpa_gui gui.launch.py port:=9000 read_only:=true smpa:=false
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARGUMENTS = (
    ('host', '127.0.0.1', 'Listening address of the web server; keep the loopback default unless you need remote viewing.'),
    ('port', '8765', 'Listening port of the web server.'),
    ('read_only', 'false', 'true disables action goals, parameter changes and service calls.'),
    ('keep_goals_on_exit', 'false', 'true leaves active action goals running when the GUI stops.'),
    ('smpa', 'true', 'false disables the optional SMPA integration.'),
    ('smpa_memory_root', '', "SMPA's memory directory for the SMPA tab (read-only); empty for the default."),
)


def is_true(text):
    """Interpret a launch argument as yes or no: 'true', '1' and 'yes' (any case) mean yes."""
    return text.strip().lower() in ('true', '1', 'yes')


def command_arguments(value):
    """Translate the launch argument values into the command-line options of the ``smpa_gui`` executable.

    ``value`` maps each launch argument name to its text. Address and port
    are always passed; the flags only when they are switched on. The memory
    directory is passed only when SMPA is enabled and a directory was given.
    """
    arguments = ['--host', value['host'], '--port', value['port']]
    if is_true(value['read_only']):
        arguments.append('--read-only')
    if is_true(value['keep_goals_on_exit']):
        arguments.append('--keep-goals-on-exit')
    if not is_true(value['smpa']):
        arguments.append('--no-smpa')
    elif value['smpa_memory_root'].strip():
        arguments += ['--memory-root', value['smpa_memory_root'].strip()]
    return arguments


def gui_process(context):
    """Build the GUI node action from the launch arguments, once their values are known.

    Read every argument from the launch context, translate the values into
    command-line options, and start the ``smpa_gui`` executable with them.
    The process is given time to settle goals and leave the ROS graph when
    the launch is stopped.
    """
    value = {name: LaunchConfiguration(name).perform(context) for name, _, _ in ARGUMENTS}
    arguments = command_arguments(value)
    return [Node(package='smpa_gui', executable='smpa_gui', arguments=arguments, output='screen',
                 sigterm_timeout='15', sigkill_timeout='5')]


def generate_launch_description():
    """Declare the GUI's launch arguments and start the GUI process with them."""
    declarations = [DeclareLaunchArgument(name, default_value=default, description=description)
                    for name, default, description in ARGUMENTS]
    return LaunchDescription(declarations + [OpaqueFunction(function=gui_process)])
