"""Build description of the ``smpa_gui`` ament_python package.

Concepts:
    Python code is installed as the ``smpa_gui`` package. The browser
    frontend (HTML, JavaScript, CSS and the vendored Cytoscape library) is
    installed as package data inside it, so the server finds its assets
    beside its own code in a source checkout and in an install space alike.
    The launch file, the package manifest and the ament index marker go to
    ``share/smpa_gui``, which is what ``ros2 pkg`` and ``ros2 launch`` use.
"""
from pathlib import Path

from setuptools import find_packages, setup

PACKAGE = 'smpa_gui'
HERE = Path(__file__).resolve().parent


def frontend_files():
    """List every frontend file relative to the Python package, for installation as package data.

    Walk the frontend folder and keep each regular file, so a new view or
    viewer is installed without editing this list. Paths are relative to
    the ``smpa_gui`` Python package, as setuptools expects.
    """
    base = HERE / PACKAGE
    return sorted(str(path.relative_to(base)) for path in (base / 'frontend').rglob('*') if path.is_file())


setup(
    name=PACKAGE,
    version='0.5.0',
    packages=find_packages(include=[PACKAGE, PACKAGE + '.*']),
    package_data={PACKAGE: frontend_files()},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + PACKAGE]),
        ('share/' + PACKAGE, ['package.xml']),
        ('share/' + PACKAGE + '/launch', ['launch/gui.launch.py']),
    ],
    install_requires=['setuptools'],
    zip_safe=False,
    maintainer='Mark Adamik',
    maintainer_email='dorteel@gmail.com',
    description='Standalone browser GUI for ROS 2 with optional SMPA views.',
    license='MIT',
    entry_points={'console_scripts': ['smpa_gui = smpa_gui.server:main']},
)
