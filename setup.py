import setuptools
import os
import re

with open("README.md", "r") as fh:
    long_description = fh.read()

def get_version(package):
    """
    Return package version as listed in `__version__` in `__init__.py`.
    """
    path = os.path.join(os.path.dirname(__file__), package, "__init__.py")
    with open(path, "rb") as f:
        init_py = f.read().decode("utf-8")
    return re.search("__version__ = ['\"]([^'\"]+)['\"]", init_py).group(1)


install_requirements = [
    'cyanodbc >= 0.0.3',
    'prompt_toolkit >= 3.0.5',
    'Pygments>=2.6.1',
    'sqlparse >= 0.3.1',
    'configobj >= 5.0.6',
    'click >= 7.1.2',
    'cli_helpers >= 2.0.1'
]

setuptools.setup(
    name = "odbcli",
    version = get_version("odbcli"),
    author = "Oliver Gjoneski",
    author_email = "ogjoneski@gmail.com",
    description = "ODBC Client",
    license = 'BSD-3',
    long_description = long_description,
    long_description_content_type = "text/markdown",
    install_requires = install_requirements,
    url = "https://github.com/pypa/odbc-cli",
    scripts=[
        'odbc-cli'
        ],
    packages = setuptools.find_packages(),
    include_package_data = True,
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
    ],
    # As python prompt toolkit
    python_requires = '>=3.6.1',
)
