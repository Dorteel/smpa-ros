cmake_minimum_required(VERSION 3.8)
project(smpa_ros_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(action_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/DescribeScene.srv"
  "action/Listen.action"
  "action/Talk.action"
  DEPENDENCIES sensor_msgs action_msgs
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
