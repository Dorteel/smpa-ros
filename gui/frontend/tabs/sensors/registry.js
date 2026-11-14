/**
 * The sensor viewer registry: which frontend module draws which kind of sensor data.
 *
 * Concepts: the gateway's registry maps ROS message types to a viewer name
 * (for example "pointcloud"). This registry maps each viewer name to the
 * module that draws it. A viewer module exports create(container), which
 * returns an object with update(header, binary), reset() and destroy().
 * Supporting a new sensor type means adding an encoder in the gateway and
 * one entry here; the Sensors tab itself does not change.
 */
import * as image from './viewers/image.js';
import * as pointcloud from './viewers/pointcloud.js';
import * as laserscan from './viewers/laserscan.js';
import * as imu from './viewers/imu.js';
import * as jointstate from './viewers/jointstate.js';
import * as navsatfix from './viewers/navsatfix.js';
import * as range from './viewers/range.js';
import * as camerainfo from './viewers/camerainfo.js';

export const VIEWERS = {
  image: {label: 'Camera', module: image},
  pointcloud: {label: 'Point cloud (3D)', module: pointcloud},
  laserscan: {label: 'Laser scan (2D)', module: laserscan},
  imu: {label: 'IMU', module: imu},
  jointstate: {label: 'Joint states', module: jointstate},
  navsatfix: {label: 'Satellite position', module: navsatfix},
  range: {label: 'Range', module: range},
  camerainfo: {label: 'Camera calibration', module: camerainfo},
};
