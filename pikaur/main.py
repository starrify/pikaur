#!/usr/bin/python3
# -*- coding: utf-8 -*-

""" This file is licensed under GPLv3, see https://www.gnu.org/licenses/ """

import os
import sys
import readline
import signal
import subprocess
import codecs
import shutil
import atexit
import io
from typing import List
from time import sleep
from argparse import ArgumentError  # pylint: disable=no-name-in-module
from multiprocessing.pool import ThreadPool

from .i18n import _  # keep that first
from .args import (
    parse_args, reconstruct_args, cli_print_help
)
from .core import (
    InstallInfo,
    spawn, interactive_spawn, running_as_root, remove_dir, sudo, isolate_root_cmd,
)
from .pprint import color_line, bold_line, print_stderr, print_stdout
from .print_department import (
    pretty_format_upgradeable, print_version, print_not_found_packages,
)
from .updates import find_repo_upgradeable, find_aur_updates
from .prompt import ask_to_continue
from .config import (
    BUILD_CACHE_PATH, PACKAGE_CACHE_PATH, CACHE_ROOT,
    PikaurConfig,
)
from .exceptions import SysExit
from .pikspect import TTYRestore
from .install_cli import InstallPackagesCLI
from .search_cli import cli_search_packages
from .info_cli import cli_info_packages
from .aur import find_aur_packages, get_repo_url
from .aur_deps import get_aur_deps_list
from .pacman import PackageDB, PackagesNotFoundInRepo


SUDO_LOOP_INTERVAL = 1


def init_readline() -> None:
    # follow GNU readline config in prompts:
    system_inputrc_path = '/etc/inputrc'
    if os.path.exists(system_inputrc_path):
        readline.read_init_file(system_inputrc_path)
    user_inputrc_path = os.path.expanduser('~/.inputrc')
    if os.path.exists(user_inputrc_path):
        readline.read_init_file(user_inputrc_path)


init_readline()


def init_output_encoding() -> None:
    for attr in ('stdout', 'stderr'):
        real_stream = getattr(sys, attr)
        try:
            setattr(
                sys, attr,
                codecs.open(
                    real_stream.fileno(),
                    mode='w', buffering=0, encoding='utf-8'
                )
            )
        except io.UnsupportedOperation:
            pass
        else:
            getattr(sys, attr).buffer = real_stream.buffer


init_output_encoding()


def cli_print_upgradeable() -> None:
    args = parse_args()
    updates: List[InstallInfo] = []
    if not args.repo:
        aur_updates, _not_found_aur_pkgs = find_aur_updates()
        updates += aur_updates
    if not args.aur:
        updates += find_repo_upgradeable()
    if args.quiet:
        print_stdout('\n'.join([
            pkg_update.name for pkg_update in updates
        ]))
    else:
        print_stdout(pretty_format_upgradeable(
            updates,
            print_repo=PikaurConfig().sync.get_bool('AlwaysShowPkgOrigin')
        ))


def sudo_loop(once=False) -> None:
    """
    get sudo for further questions (command should do nothing)
    """
    while True:
        interactive_spawn(sudo([PikaurConfig().misc.PacmanPath, '-T']))
        if once:
            break
        sleep(SUDO_LOOP_INTERVAL)


def cli_install_packages() -> None:

    def _run_install() -> None:
        InstallPackagesCLI()

    if running_as_root():
        _run_install()
    else:
        sudo_loop(once=True)
        with ThreadPool(processes=2) as pool:
            install_packages_thread = pool.apply_async(_run_install, ())
            pool.apply_async(sudo_loop)
            pool.close()
            catched_exc = None
            try:
                install_packages_thread.get()
            except Exception as exc:
                catched_exc = exc
            finally:
                pool.terminate()
            if catched_exc:
                raise catched_exc  # pylint: disable=raising-bad-type


def cli_pkgbuild() -> None:
    cli_install_packages()


def cli_getpkgbuild() -> None:
    args = parse_args()
    pwd = os.path.abspath(os.path.curdir)
    aur_pkg_names = args.positional

    aur_pkgs, not_found_aur_pkgs = find_aur_packages(aur_pkg_names)
    repo_pkgs = []
    not_found_repo_pkgs = []
    for pkg_name in not_found_aur_pkgs:
        try:
            repo_pkg = PackageDB.find_repo_package(pkg_name)
        except PackagesNotFoundInRepo:
            not_found_repo_pkgs.append(pkg_name)
        else:
            repo_pkgs.append(repo_pkg)

    if not_found_repo_pkgs:
        print_not_found_packages(not_found_repo_pkgs)

    if args.deps:
        aur_pkgs = aur_pkgs + get_aur_deps_list(aur_pkgs)

    for aur_pkg in aur_pkgs:
        name = aur_pkg.name
        repo_path = os.path.join(pwd, name)
        print_stdout()
        interactive_spawn([
            'git',
            'clone',
            get_repo_url(aur_pkg.packagebase),
            repo_path,
        ])

    for repo_pkg in repo_pkgs:
        print_stdout()
        interactive_spawn([
            'asp',
            'checkout',
            repo_pkg.name,
        ])


def cli_clean_packages_cache() -> None:
    args = parse_args()
    if not args.repo:
        for directory, message, minimal_clean_level in (
                (BUILD_CACHE_PATH, "Build directory", 1, ),
                (PACKAGE_CACHE_PATH, "Packages directory", 2, ),
        ):
            if minimal_clean_level <= args.clean and os.path.exists(directory):
                print_stdout('\n' + _("{}: {}").format(message, directory))
                if ask_to_continue(text='{} {}'.format(
                        color_line('::', 12),
                        _("Do you want to remove all files?")
                )):
                    remove_dir(directory)
    if not args.aur:
        sys.exit(
            interactive_spawn(sudo(
                [PikaurConfig().misc.PacmanPath, ] + reconstruct_args(args, ['--repo'])
            )).returncode
        )


def cli_print_version() -> None:
    args = parse_args()
    pacman_version = spawn(
        [PikaurConfig().misc.PacmanPath, '--version', ],
    ).stdout_text.splitlines()[1].strip(' .-')
    print_version(pacman_version, quiet=args.quiet)


def cli_entry_point() -> None:
    # pylint: disable=too-many-branches

    # operations are parsed in order what the less destructive (like info and query)
    # are being handled first, for cases when user by mistake
    # specified both operations, like `pikaur -QS smth`

    args = parse_args()
    raw_args = args.raw

    not_implemented_in_pikaur = False
    require_sudo = True

    if args.help:
        cli_print_help()
    elif args.version:
        cli_print_version()

    elif args.query:
        if args.sysupgrade:
            cli_print_upgradeable()
        else:
            not_implemented_in_pikaur = True
            require_sudo = False

    elif args.getpkgbuild:
        cli_getpkgbuild()

    elif args.pkgbuild:
        cli_pkgbuild()

    elif args.sync:
        if args.search:
            cli_search_packages()
        elif args.info:
            cli_info_packages()
        elif args.clean:
            cli_clean_packages_cache()
        elif args.sysupgrade or '-S' in raw_args or '-Sy' in raw_args:
            cli_install_packages()
        elif args.groups:
            not_implemented_in_pikaur = True
            require_sudo = False
        else:
            not_implemented_in_pikaur = True

    else:
        not_implemented_in_pikaur = True

    if not_implemented_in_pikaur:
        if require_sudo and raw_args:
            sys.exit(
                interactive_spawn(sudo([PikaurConfig().misc.PacmanPath, ] + raw_args)).returncode
            )
        sys.exit(
            interactive_spawn([PikaurConfig().misc.PacmanPath, ] + raw_args).returncode
        )


def check_systemd_dynamic_users() -> bool:
    try:
        out = subprocess.check_output(['systemd-run', '--version'],
                                      universal_newlines=True)
    except FileNotFoundError:
        return False
    first_line = out.split('\n')[0]
    version = int(first_line.split()[1])
    return version >= 235


def check_runtime_deps():
    if running_as_root() and not check_systemd_dynamic_users():
        print_stderr("{} {}".format(
            color_line('::', 9),
            _("pikaur requires systemd >= 235 (dynamic users) to be run as root."),
        ))
        sys.exit(65)
    for dep_bin in [
            "fakeroot",
    ] + (['sudo'] if not running_as_root() else []):
        if not shutil.which(dep_bin):
            print_stderr("{} '{}' {}.".format(
                color_line(':: ' + _('error') + ':', 9),
                bold_line(dep_bin),
                "executable not found"
            ))
            sys.exit(2)


def create_dirs() -> None:
    if running_as_root():
        # Let systemd-run setup the directories and symlinks
        true_cmd = isolate_root_cmd(['true'])
        result = spawn(true_cmd)
        if result.returncode != 0:
            raise Exception(result)
        # Chown the private CacheDirectory to root to signal systemd that
        # it needs to recursively chown it to the correct user
        os.chown(os.path.realpath(CACHE_ROOT), 0, 0)
    if not os.path.exists(CACHE_ROOT):
        os.makedirs(CACHE_ROOT)


def restore_tty():
    TTYRestore.restore()


def handle_sig_int(*_whatever):
    print_stderr("\n\nCanceled by user (SIGINT)")
    sys.exit(125)


def main() -> None:
    try:
        args = parse_args()
    except ArgumentError as exc:
        print_stderr(exc)
        sys.exit(22)
    check_runtime_deps()

    create_dirs()
    # initialize config to avoid race condition in threads:
    PikaurConfig.get_config()

    atexit.register(restore_tty)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    if not args.debug:
        signal.signal(signal.SIGINT, handle_sig_int)

    try:
        cli_entry_point()
    except BrokenPipeError:
        # @TODO: should it be 32?
        sys.exit(0)
    except SysExit as exc:
        sys.exit(exc.code)
    sys.exit(0)


if __name__ == '__main__':
    main()
