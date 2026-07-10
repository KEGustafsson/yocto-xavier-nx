# Auto-imported by Python's `site` module when this directory is on
# PYTHONPATH (see scripts/03-build.sh).
#
# Kirkstone's bitbake (bb/asyncrpc/serv.py AsyncServer.serve_as_process)
# spawns its hash-equivalence server with multiprocessing.Process(target=run)
# where `run` is a function local to another function. That's only
# picklable with the "fork" start method. Python 3.14 changed the POSIX
# default from "fork" to "forkserver", which pickles the target and fails
# with `_pickle.PicklingError: Can't pickle local object ...`. kirkstone
# predates that change, so restore the old default for the bitbake process.
import multiprocessing

multiprocessing.set_start_method("fork", force=True)
