"""Install the package identity and its primary ROS configuration resource."""
from setuptools import find_packages, setup

setup(
    name='smpa_llm',
    version='0.0.1',
    packages=find_packages(),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/smpa_llm']),
        ('share/smpa_llm', ['package.xml', 'config.yaml']),
    ],
    install_requires=['setuptools>=30.3,<80', 'outlines==1.3.3', 'jsonschema==4.26.0',
                      'openai==3.39.0', 'python-dotenv==1.2.4'],
    maintainer='Mark Adamik',
    maintainer_email='dorteel@gmail.com',
    description='Shared inference and offline training package boundary for SMPA.',
    license='MIT',
    zip_safe=True,
)
