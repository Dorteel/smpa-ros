from glob import glob
from setuptools import find_packages, setup

package_name = 'smpa_ros'

setup(
    name=package_name,
    version='0.0.1',
    py_modules=['runner', 'memory'],
    packages=find_packages(include=['smpa_ros', 'smpa_ros.*', 'speech', 'speech.*']),
    package_data={'smpa_ros': ['prompts/*/*.txt', 'conversation.html', 'belief-structure.owl']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml', 'config.yaml']),
        ('share/' + package_name + '/models', glob('models/*.yaml')),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools>=30.3,<80', 'outlines==1.3.3', 'jsonschema==4.26.0',
                      'openai==3.39.0', 'python-dotenv==1.2.4', 'rdflib==7.1.4'],
    zip_safe=True,
    maintainer='Mark Adamik',
    maintainer_email='maintainer@example.com',
    description='Minimal Sense–Model–Plan–Act research architecture.',
    license='MIT',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'sense = smpa_ros.sense:main',
        'conversation = smpa_ros.conversation_web:main',
        'smpa = smpa_ros.smpa:main',
        'listen = speech.actions:listen_main',
        'talk = speech.actions:talk_main',
        'listener_talker = speech.demo:main',
    ]},
)
