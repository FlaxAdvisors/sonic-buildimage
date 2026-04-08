"""Sphinx configuration for Wedge 100S-32X platform documentation."""

project = 'Wedge 100S-32X SONiC Platform'
copyright = '2026, FlaxAdvisors'
author = 'FlaxAdvisors'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
]

napoleon_google_docstring = True
napoleon_numpy_docstring = False

templates_path = []
exclude_patterns = ['_build']

html_theme = 'alabaster'

import os, sys
sys.path.insert(0, os.path.abspath('../sonic_platform'))
