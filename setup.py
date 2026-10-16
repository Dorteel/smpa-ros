from glob import glob
from setuptools import find_packages, setup

package_name = 'smpa_ros'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(include=['smpa_ros', 'smpa_ros.*']),
    package_data={'smpa_ros': ['*/prompts/*.txt', '*/models/*.json']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/models', glob('models/*.yaml')),
        ('share/' + package_name + '/config', ['config/pipeline.yaml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Mark Adamik',
    maintainer_email='maintainer@example.com',
    description='Minimal Sense–Model–Plan–Act research architecture.',
    license='MIT',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'hello = smpa_ros.hello:main',
        'sense = smpa_ros.sense.service:main',
        'loop = smpa_ros.loop:main',
    ]},
)
