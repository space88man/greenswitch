#!/usr/bin/env python
# -*- coding: utf-8 -*-


from setuptools import setup, find_packages


with open('README.rst') as f:
    readme = f.read()

with open('requirements.txt') as f:
    requires = f.readlines()

setup(
    name='trioswitch',
    version='2020.6.0.dev2',
    description='Async rewrite of the "Battle proven FreeSWITCH Event Socket Protocol client implementation" GreenSWITCH',
    long_description=readme,
    author='Shih-Ping Chan',
    author_email='shihping.chan@gmail.com',
    # url=u'https://github.com/evoluxbr/greenswitch',
    license='MIT',
    packages=find_packages(exclude=('tests', 'docs')),
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Programming Language :: Python',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.8',
    ],
    install_requires=requires
)
