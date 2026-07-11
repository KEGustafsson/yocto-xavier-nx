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

# Python 3.12 removed the deprecated ast.Str node (and Constant.s alias),
# unifying string literals into plain ast.Constant. kirkstone's
# oe.license.LicenseVisitor (meta/lib/oe/license.py) still does
# `isinstance(node, ast.Str)` / `node.s`, which now raises
# AttributeError: module 'ast' has no attribute 'Str', failing any
# do_rootfs/do_populate_lic task that parses a package's LICENSE string.
# Restore both as compatibility shims.
import ast


class _StrMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, ast.Constant) and isinstance(instance.value, str)


class _Str(ast.AST, metaclass=_StrMeta):
    pass


if not hasattr(ast, "Str"):
    ast.Str = _Str
    ast.Constant.s = property(lambda self: self.value)
