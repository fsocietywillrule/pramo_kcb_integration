from pathlib import Path

from setuptools import find_packages, setup


with open("requirements.txt") as f:
    install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]


version = {}
init_file = Path("pramo_kcb_integration/__init__.py")
exec(init_file.read_text(), version)


setup(
    name="pramo_kcb_integration",
    version=version.get("__version__", "0.1.0"),
    description="KCB Buni integration for ERPNext",
    author="Pramo Traders",
    author_email="accounts@finspreeglobal.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)

