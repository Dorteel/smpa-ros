cmake_minimum_required(VERSION 3.8)
project(smpa_ros_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(action_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/DescribeScene.srv"
  "srv/EvaluateVisualDecision.srv"
  "srv/SegmentImage.srv"
  "action/Listen.action"
  "action/Talk.action"
  "action/GenerateText.action"
  "action/GenerateStructured.action"
  "action/DescribeImage.action"
  "action/LoadModel.action"
  "action/UnloadModel.action"
  "action/SetActiveModel.action"
  "srv/ListModels.srv"
  "srv/GetModelStatus.srv"
  "srv/GetSmpaStatus.srv"
  "srv/StartSmpa.srv"
  "srv/StopSmpa.srv"
  "msg/InferenceStatus.msg"
  "msg/SmpaEvent.msg"
  DEPENDENCIES sensor_msgs action_msgs
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
