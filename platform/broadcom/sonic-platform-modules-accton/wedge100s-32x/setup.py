#!/usr/bin/env python

import os
import sys
from setuptools import setup
os.listdir

setup(
   name='wedge100s_32x',
   version='1.0',
   description='Module to initialize Accton WEDGE100S-32X platforms',
   
   packages=['wedge100s_32x'],
   package_dir={'wedge100s_32x': 'wedge100s_32x/classes'},
)

