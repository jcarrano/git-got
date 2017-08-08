from setuptools import setup

def readme():
    with open('README.md') as f:
        return f.read()

setup(name='git-got',
      version='1.5.1',
      description='Simple look-aside cache for git or whatever',
      long_description=readme(),
      classifiers=[
        'Programming Language :: Python :: 2',
      ],
      keywords='git',
      url='https://github.com/jake4679/git-got',
      author='Jake Cheuvront',
      #author_email='???',
      license='BSD',
      py_modules=['git_got'],
      entry_points = {
        'console_scripts': ['git-got=git_got:main'],
      },
      install_requires=[
          'dulwich',
          'paramiko',
          'pycurl',
          'requests',
          'requests-toolbelt'
      ],
      include_package_data=True,
      zip_safe=True
)
