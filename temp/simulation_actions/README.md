# Temporary Webots action integration

No ROS package or server implementation is vendored here. SMPA reuses and builds
`simulation_actions` from the adjacent Webots checkout, avoiding duplicate ROS
package names and copied action definitions.

Source: https://github.com/Dorteel/webots_ros2_simulation/tree/master/simulation_actions
Inspected revision: `f8a975f1572e248f1ab7614bbc69e9ff6fc619e9` (Apache-2.0).

- `/look_left` and `/look_right`: `simulation_actions/action/Look`.
  `angle=0.0` selects the server default (30 degrees in the inspected configuration).
  Result: `success`, `message`, `achieved_angle`; feedback: `remaining_angle`.
- `/look_ahead`: `simulation_actions/action/LookAhead` (empty goal).
  Built with the upstream package and available for manual use; the existing
  SMPA planner continues to select only left/right.

`install.sh` explicitly builds this upstream source at workspace level and
reconfigures a stale CMake cache pointing at an older checkout. Set
`SIMULATION_ACTIONS_SOURCE` only if the source is elsewhere. `run_smpa.sh`
checks generated Python/native type support, not merely the source `.action` file.
No simulation files are changed and no servers are launched by SMPA installation.

See **Temporary Webots Integration** in the main README for commands and evidence.
To remove this temporary integration later, remove this directory and the Webots
source selection/build and preflight blocks in the scripts after replacing the
existing fixed action registry/executor with an explicitly authorized alternative.
Until then, removing the generated interface breaks the existing Look executor.
