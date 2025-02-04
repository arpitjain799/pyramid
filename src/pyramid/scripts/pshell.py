import argparse
from code import interact
from contextlib import contextmanager
import os
import pkg_resources
import sys
import textwrap

from pyramid.paster import bootstrap
from pyramid.scripts.common import get_config_loader, parse_vars
from pyramid.settings import aslist
from pyramid.util import DottedNameResolver, make_contextmanager


def main(argv=sys.argv, quiet=False):
    command = PShellCommand(argv, quiet)
    return command.run()


def python_shell_runner(env, help, interact=interact):
    cprt = 'Type "help" for more information.'
    banner = f"Python {sys.version} on {sys.platform}\n{cprt}"
    banner += '\n\n' + help + '\n'
    interact(banner, local=env)


class PShellCommand:
    description = """\
    Open an interactive shell with a Pyramid app loaded.  This command
    accepts one positional argument named "config_uri" which specifies the
    PasteDeploy config file to use for the interactive shell. The format is
    "inifile#name". If the name is left off, the Pyramid default application
    will be assumed.  Example: "pshell myapp.ini#main".

    If you do not point the loader directly at the section of the ini file
    containing your Pyramid application, the command will attempt to
    find the app for you. If you are loading a pipeline that contains more
    than one Pyramid application within it, the loader will use the
    last one.
    """
    bootstrap = staticmethod(bootstrap)  # for testing
    get_config_loader = staticmethod(get_config_loader)  # for testing
    pkg_resources = pkg_resources  # for testing

    parser = argparse.ArgumentParser(
        description=textwrap.dedent(description),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-p',
        '--python-shell',
        action='store',
        dest='python_shell',
        default='',
        help=(
            'Select the shell to use. A list of possible '
            'shells is available using the --list-shells '
            'option.'
        ),
    )
    parser.add_argument(
        '-l',
        '--list-shells',
        dest='list',
        action='store_true',
        help='List all available shells.',
    )
    parser.add_argument(
        '--setup',
        dest='setup',
        help=(
            "A callable that will be passed the environment "
            "before it is made available to the shell. This "
            "option will override the 'setup' key in the "
            "[pshell] ini section."
        ),
    )
    parser.add_argument(
        'config_uri',
        nargs='?',
        default=None,
        help='The URI to the configuration file.',
    )
    parser.add_argument(
        'config_vars',
        nargs='*',
        default=(),
        help="Variables required by the config file. For example, "
        "`http_port=%%(http_port)s` would expect `http_port=8080` to be "
        "passed here.",
    )

    default_runner = python_shell_runner  # testing

    loaded_objects = {}
    object_help = {}
    preferred_shells = []
    setup = None
    pystartup = os.environ.get('PYTHONSTARTUP')
    resolver = DottedNameResolver(None)

    def __init__(self, argv, quiet=False):
        self.quiet = quiet
        self.args = self.parser.parse_args(argv[1:])

    def pshell_file_config(self, loader, defaults):
        settings = loader.get_settings('pshell', defaults)
        self.loaded_objects = {}
        self.object_help = {}
        self.setup = None
        for k, v in settings.items():
            if k == 'setup':
                self.setup = v
            elif k == 'default_shell':
                self.preferred_shells = [x.lower() for x in aslist(v)]
            else:
                self.loaded_objects[k] = self.resolver.maybe_resolve(v)
                self.object_help[k] = v

    def out(self, msg):  # pragma: no cover
        if not self.quiet:
            print(msg)

    def run(self, shell=None):
        if self.args.list:
            return self.show_shells()
        if not self.args.config_uri:
            self.out('Requires a config file argument')
            return 2

        config_uri = self.args.config_uri
        config_vars = parse_vars(self.args.config_vars)
        loader = self.get_config_loader(config_uri)
        loader.setup_logging(config_vars)
        self.pshell_file_config(loader, config_vars)

        self.env = self.bootstrap(config_uri, options=config_vars)

        # remove the closer from the env
        self.closer = self.env.pop('closer')

        try:
            if shell is None:
                try:
                    shell = self.make_shell()
                except ValueError as e:
                    self.out(str(e))
                    return 1

            with self.setup_env():
                shell(self.env, self.help)

        finally:
            self.closer()

    @contextmanager
    def setup_env(self):
        # setup help text for default environment
        env = self.env
        env_help = dict(env)
        env_help['app'] = 'The WSGI application.'
        env_help['root'] = 'Root of the default resource tree.'
        env_help['registry'] = 'Active Pyramid registry.'
        env_help['request'] = 'Active request object.'
        env_help[
            'root_factory'
        ] = 'Default root factory used to create `root`.'

        # load the pshell section of the ini file
        env.update(self.loaded_objects)

        # eliminate duplicates from env, allowing custom vars to override
        for k in self.loaded_objects:
            if k in env_help:
                del env_help[k]

        # override use_script with command-line options
        if self.args.setup:
            self.setup = self.args.setup

        if self.setup:
            # call the setup callable
            self.setup = self.resolver.maybe_resolve(self.setup)

        # store the env before muddling it with the script
        orig_env = env.copy()
        setup_manager = make_contextmanager(self.setup)
        with setup_manager(env):
            # remove any objects from default help that were overidden
            for k, v in env.items():
                if k not in orig_env or v is not orig_env[k]:
                    if getattr(v, '__doc__', False):
                        env_help[k] = v.__doc__.replace("\n", " ")
                    else:
                        env_help[k] = v
            del orig_env

            # generate help text
            help = ''
            if env_help:
                help += 'Environment:'
                for var in sorted(env_help.keys()):
                    help += '\n  %-12s %s' % (var, env_help[var])

            if self.object_help:
                help += '\n\nCustom Variables:'
                for var in sorted(self.object_help.keys()):
                    help += '\n  %-12s %s' % (var, self.object_help[var])

            if self.pystartup and os.path.isfile(self.pystartup):
                with open(self.pystartup, 'rb') as fp:
                    exec(fp.read().decode('utf-8'), env)
                if '__builtins__' in env:
                    del env['__builtins__']

            self.help = help.strip()
            yield

    def show_shells(self):
        shells = self.find_all_shells()
        sorted_names = sorted(shells.keys(), key=lambda x: x.lower())

        self.out('Available shells:')
        for name in sorted_names:
            self.out(f'  {name}')
        return 0

    def find_all_shells(self):
        pkg_resources = self.pkg_resources

        shells = {}
        for ep in pkg_resources.iter_entry_points('pyramid.pshell_runner'):
            name = ep.name
            shell_factory = ep.load()
            shells[name] = shell_factory
        return shells

    def make_shell(self):
        shells = self.find_all_shells()

        shell = None
        user_shell = self.args.python_shell.lower()

        if not user_shell:
            preferred_shells = self.preferred_shells
            if not preferred_shells:
                # by default prioritize all shells above python
                preferred_shells = [k for k in shells.keys() if k != 'python']
            max_weight = len(preferred_shells)

            def order(x):
                # invert weight to reverse sort the list
                # (closer to the front is higher priority)
                try:
                    return preferred_shells.index(x[0].lower()) - max_weight
                except ValueError:
                    return 1

            sorted_shells = sorted(shells.items(), key=order)

            if len(sorted_shells) > 0:
                shell = sorted_shells[0][1]

        else:
            runner = shells.get(user_shell)

            if runner is not None:
                shell = runner

            if shell is None:
                raise ValueError(
                    'could not find a shell named "%s"' % user_shell
                )

        if shell is None:
            # should never happen, but just incase entry points are borked
            shell = self.default_runner

        return shell


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main() or 0)
