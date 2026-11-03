import re
from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as fh:
    long_description = fh.read()

with open("codetree/__init__.py", encoding="utf-8") as fh:
    version = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)

setup(
    name="codetree-cli",
    version=version,
    author="Reza Mardani",
    author_email="mardanireza30@gmail.com",
    description="See your codebase as a living, animated tree: lines of code, comments and languages per folder and file.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/rezamardani01/codetree",
    packages=find_packages(exclude=["tests", "tests.*"]),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Environment :: X11 Applications",
        "Environment :: Win32 (MS Windows)",
        "Topic :: Software Development",
    ],
    python_requires=">=3.8",
    install_requires=[],
    entry_points={
        "console_scripts": ["codetree=codetree.cli:main"],
        "gui_scripts": ["codetree-gui=codetree.cli:main_gui"],
    },
)
