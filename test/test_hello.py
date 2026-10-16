import rclpy

from smpa_ros.hello import main


def test_hello_logs_and_shuts_down(capfd):
    main(args=[])
    captured = capfd.readouterr()
    assert 'Hello ROS 2!' in captured.out + captured.err
    assert not rclpy.ok()
