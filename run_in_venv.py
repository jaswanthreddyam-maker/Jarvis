import sys
import os
import site
import runpy

venv_dir = os.path.abspath(".venv")
site_packages = os.path.join(venv_dir, "Lib", "site-packages")
site.addsitedir(site_packages)
# Also prepend to sys.path so it overrides global packages
sys.path.insert(0, site_packages)

if len(sys.argv) > 1:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    if script == "-m":
        runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
    else:
        runpy.run_path(script, run_name="__main__")
else:
    print("usage: run_in_venv.py <script_or_-m> [args]")
