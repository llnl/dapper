import os
import subprocess
import subprocess as sp
from subprocess import run as sproot_run


def positives():
    # Should match: "os.system"
    os.system("ls -l /tmp")

    # Should match: "os.system"
    os.system("echo from os.system")

    # Should match: "subprocess.run"
    subprocess.run(["echo", "from subprocess.run"])


    # Depending on your resolver, these MAY or MAY NOT match
    # (aliases/from-imports). Keep them if you want extra coverage.
    sp.run("echo from sp.run", shell=True)       # alias of subprocess
    sproot_run(["echo", "from from-import"])     # from-import alias

def negatives():
    # Should NOT match (not in the allowlist)
    subprocess.check_call(["echo", "should NOT match"])
    os.path.join("a", "b")  # not a syscall
    run("rm -rf /tmp/somewhere")

if __name__ == "__main__":
    positives()
    negatives()