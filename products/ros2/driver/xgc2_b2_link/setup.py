from setuptools import find_packages, setup

package_name = "xgc2_b2_link"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/contract", ["contract/zenoh_v1.yaml"]),
        ("share/" + package_name + "/config", ["config/forwarder_d0.yaml", "config/ground_peer.yaml"]),
    ],
    install_requires=["setuptools", "PyYAML"],
    zip_safe=True,
    maintainer="XGC2",
    maintainer_email="apt@xgc2.local",
    description="B2 G3/G4 Zenoh-aligned forwarder and ground peer",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "b2_forwarder_d0 = xgc2_b2_link.forwarder_node:main",
            "b2_ground_peer = xgc2_b2_link.ground_peer:main",
            "b2_sim_publisher = xgc2_b2_link.sim_publisher:main",
        ],
    },
)
