import json
import multiprocessing
import shlex
import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Set

from .utils import ROOT, info, sh, warn, escape_attr


class Attr:
    def __init__(
        self,
        name: str,
        exists: bool,
        broken: bool,
        blacklisted: bool,
        path: Optional[str],
        drv_path: Optional[str],
        aliases: List[str] = [],
    ) -> None:
        self.name = name
        self.exists = exists
        self.broken = broken
        self.blacklisted = blacklisted
        self.path = path
        self._path_verified: Optional[bool] = None
        self.drv_path = drv_path
        self.aliases = aliases

    def was_build(self) -> bool:
        if self.path is None:
            return False

        if self._path_verified is not None:
            return self._path_verified

        res = subprocess.run(
            ["nix-store", "--verify-path", self.path], stderr=subprocess.DEVNULL
        )
        self._path_verified = res.returncode == 0
        return self._path_verified

    def is_test(self) -> bool:
        return self.name.startswith("nixosTests")


def nix_shell(attrs: List[str], cache_directory: Path) -> None:
    if len(attrs) == 0:
        info("No packages were successfully build, skip nix-shell")
    else:
        shell = cache_directory.joinpath("shell.nix")
        write_shell_expression(shell, attrs)
        sh(["nix-shell", str(shell)], cwd=cache_directory)


def _nix_eval_filter(json: Dict[str, Any]) -> List[Attr]:
    # workaround https://github.com/NixOS/ofborg/issues/269
    blacklist = set(
        ["tests.nixos-functions.nixos-test", "tests.nixos-functions.nixosTest-test"]
    )
    attr_by_path: Dict[str, Attr] = {}
    broken = []
    for name, props in json.items():
        attr = Attr(
            name=name,
            exists=props["exists"],
            broken=props["broken"],
            blacklisted=name in blacklist,
            path=props["path"],
            drv_path=props["drvPath"],
        )
        if attr.path is not None:
            other = attr_by_path.get(attr.path, None)
            if other is None:
                attr_by_path[attr.path] = attr
            else:
                if len(other.name) > len(attr.name):
                    attr_by_path[attr.path] = attr
                    attr.aliases.append(other.name)
                else:
                    other.aliases.append(attr.name)
        else:
            broken.append(attr)
    return list(attr_by_path.values()) + broken


def nix_eval(attrs: Set[str]) -> List[Attr]:
    attr_json = NamedTemporaryFile(mode="w+", delete=False)
    delete = True
    try:
        json.dump(list(attrs), attr_json)
        eval_script = str(ROOT.joinpath("nix/evalAttrs.nix"))
        attr_json.flush()
        cmd = ["nix", "eval", "--json", f"(import {eval_script} {attr_json.name})"]

        try:
            nix_eval = subprocess.run(cmd, check=True, stdout=subprocess.PIPE)
        except subprocess.CalledProcessError:
            warn(
                f"{' '.join(cmd)} failed to run, {attr_json.name} was stored inspection"
            )
            delete = False
            raise

        return _nix_eval_filter(json.loads(nix_eval.stdout))
    finally:
        attr_json.close()
        if delete:
            os.unlink(attr_json.name)


def nix_build(attr_names: Set[str], args: str, cache_directory: Path) -> List[Attr]:
    if not attr_names:
        info("Nothing changed")
        return []

    attrs = nix_eval(attr_names)
    filtered = []
    for attr in attrs:
        if not (attr.broken or attr.blacklisted):
            filtered.append(attr.name)

    if len(filtered) == 0:
        return attrs

    build = cache_directory.joinpath("build.nix")
    write_shell_expression(build, filtered)

    command = [
        "nix",
        "build",
        "--no-link",
        "--keep-going",
        # only matters for single-user nix and trusted users
        "--max-jobs",
        str(multiprocessing.cpu_count()),
        "--option",
        "build-use-sandbox",
        "true",
        "-f",
        str(build),
    ] + shlex.split(args)

    try:
        sh(command)
    except subprocess.CalledProcessError:
        pass
    return attrs


def write_shell_expression(filename: Path, attrs: List[str]) -> None:
    with open(filename, "w+") as f:
        f.write(
            """{ pkgs ? import ./nixpkgs {} }:
with pkgs;
stdenv.mkDerivation {
  name = "env";
  buildInputs = [
"""
        )
        f.write("\n".join(f"    {escape_attr(a)}" for a in attrs))
        f.write(
            """
  ];
  unpackPhase = ":";
  installPhase = "touch $out";
}
"""
        )
